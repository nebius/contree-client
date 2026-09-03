import assert from "node:assert/strict";
import { mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import {
  fromEnv,
  loadProfiles,
  parseIni,
  ProfileError,
  resolveProfile,
} from "../lib/profiles.js";
import { ContreeError } from "../lib/errors.js";

test("parseIni treats [DEFAULT] as the defaults section", () => {
  const sections = parseIni(
    "[DEFAULT]\nprofile = staging\nproject = shared\n[profile:staging]\ntoken = t\n",
  );
  assert.equal(sections.get("").profile, "staging");
  assert.equal(sections.get("").project, "shared");
  assert.equal(sections.get("profile:staging").token, "t");
  assert.equal(sections.has("DEFAULT"), false);
});

test("parseIni cannot pollute prototypes", () => {
  const sections = parseIni(
    "[__proto__]\npolluted = yes\n[profile:x]\n__proto__ = nope\nconstructor = nope\n",
  );
  assert.equal({}.polluted, undefined);
  assert.equal(Object.prototype.polluted, undefined);
  // the keys are still available as plain data
  assert.equal(sections.get("__proto__").polluted, "yes");
  assert.equal(sections.get("profile:x")["__proto__"], "nope");
});

async function writeConfig(files) {
  const home = await mkdtemp(join(tmpdir(), "contree-js-profiles-"));
  for (const [name, content] of Object.entries(files)) {
    await writeFile(join(home, name), content);
  }
  return join(home, "auth.ini");
}

test("the [DEFAULT] active profile is honored and inherited", async () => {
  const authPath = await writeConfig({
    "cli.ini": "[DEFAULT]\nprofile = staging\nproject = shared-project\n",
    "auth.ini":
      "[profile:default]\ntoken = default-token\nurl = https://prod.example\n" +
      "[profile:staging]\ntoken = staging-token\nurl = https://staging.example\n",
  });
  const { profiles, active } = await loadProfiles(authPath);
  assert.equal(active, "staging");
  const resolved = await resolveProfile(null, { path: authPath });
  assert.equal(resolved.name, "staging");
  assert.equal(resolved.token, "staging-token");
  assert.equal(resolved.url, "https://staging.example");
  // ConfigParser semantics: DEFAULT values show through sections
  assert.equal(resolved.project, "shared-project");
  assert.equal(profiles.default.token, "default-token");
});

test("auth.ini values win over cli.ini", async () => {
  const authPath = await writeConfig({
    "cli.ini": "[profile:default]\nurl = https://old.example\ntoken = stale\n",
    "auth.ini": "[profile:default]\ntoken = fresh\n",
  });
  const resolved = await resolveProfile("default", { path: authPath });
  assert.equal(resolved.token, "fresh");
  assert.equal(resolved.url, "https://old.example");
});

test("a missing profile raises ProfileError", async () => {
  const authPath = await writeConfig({ "auth.ini": "" });
  await assert.rejects(
    resolveProfile("nonexistent", { path: authPath }),
    (error) =>
      error instanceof ProfileError && !(error instanceof ContreeError),
  );
});

test("filesystem errors are not silenced as missing profiles", async () => {
  // a directory in place of the ini file yields EISDIR, not ENOENT
  const home = await mkdtemp(join(tmpdir(), "contree-js-profiles-"));
  await assert.rejects(loadProfiles(home), (error) => error.code !== "ENOENT");
});

test("fromEnv builds a profile from contree-cli variable names", () => {
  const saved = { ...process.env };
  try {
    for (const name of [
      "CONTREE_TOKEN",
      "NEBIUS_API_KEY",
      "CONTREE_URL",
      "CONTREE_PROJECT",
      "NEBIUS_AI_PROJECT",
    ]) {
      delete process.env[name];
    }
    assert.equal(fromEnv(), null);

    process.env.CONTREE_TOKEN = "env-token";
    assert.equal(fromEnv(), null); // a token alone is not enough

    process.env.CONTREE_URL = "https://env.example.com/";
    const profile = fromEnv();
    assert.ok(profile !== null); // non-null means it parsed
    assert.equal(profile.token, "env-token");
    assert.equal(profile.url, "https://env.example.com");
    assert.equal(profile.project, null);

    delete process.env.CONTREE_TOKEN;
    process.env.NEBIUS_API_KEY = "nebius-token";
    process.env.NEBIUS_AI_PROJECT = "nebius-project";
    const fallback = fromEnv();
    assert.equal(fallback.token, "nebius-token");
    assert.equal(fallback.project, "nebius-project");
  } finally {
    process.env = saved;
  }
});

test("CONTREE_PROFILE overrides the recorded active profile", async () => {
  const authPath = await writeConfig({
    "auth.ini":
      "[DEFAULT]\nprofile = default\n" +
      "[profile:default]\ntoken = default-token\nurl = https://prod.example\n" +
      "[profile:staging]\ntoken = staging-token\nurl = https://staging.example\n",
  });
  const saved = process.env.CONTREE_PROFILE;
  try {
    process.env.CONTREE_PROFILE = "staging";
    const resolved = await resolveProfile(null, { path: authPath });
    assert.equal(resolved.name, "staging");
    assert.equal(resolved.token, "staging-token");
  } finally {
    if (saved === undefined) {
      delete process.env.CONTREE_PROFILE;
    } else {
      process.env.CONTREE_PROFILE = saved;
    }
  }
});
