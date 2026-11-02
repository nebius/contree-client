import assert from "node:assert/strict";
import { mkdtemp, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { loadProfiles, resolveProfile, ProfileError } from "../lib/profiles.js";

test("TOML merges partial profiles and rejects malformed credentials safely", async () => {
  const home = await mkdtemp(join(tmpdir(), "contree-toml-"));
  try {
    await writeFile(
      join(home, "cli.ini"),
      "[DEFAULT]\nprofile = dev\n[profile:dev]\nurl = https://example.test/\nproject = cli-project\n",
    );
    await writeFile(
      join(home, "auth.ini"),
      "[profile:ignored]\ntoken = ignored-token\nurl = https://ignored.test\n",
    );
    const path = join(home, "e2e.toml");
    await writeFile(path, '[dev]\ntoken = "dummy%token"\n[partial]\n');
    const { profiles, active } = await loadProfiles(path);
    assert.equal(active, "dev");
    assert.deepEqual(Object.keys(profiles).sort(), ["dev", "partial"]);
    const profile = await resolveProfile("dev", { path });
    assert.equal(profile.token, "dummy%token");
    assert.equal(profile.url, "https://example.test");
    assert.equal(profile.project, "cli-project");
    await assert.rejects(resolveProfile("partial", { path }), ProfileError);
    await writeFile(path, '[dev]\ntoken = "SECRET-MARKER" broken');
    await assert.rejects(loadProfiles(path), (error) => {
      assert.ok(error instanceof ProfileError);
      assert.ok(error.message.includes(JSON.stringify(path)));
      assert.ok(!error.stack.includes("SECRET-MARKER"));
      assert.equal(error.cause, undefined);
      return true;
    });
    await assert.rejects(
      resolveProfile("dev", { path: join(home, "missing.toml") }),
      ProfileError,
    );
  } finally {
    await rm(home, { recursive: true, force: true });
  }
});

test("malformed TOML UTF-8 fails without exposing or replacing credentials", async () => {
  const home = await mkdtemp(join(tmpdir(), "contree-toml-encoding-"));
  try {
    await writeFile(
      join(home, "auth.ini"),
      "[profile:dev]\ntoken = ignored-ini-sentinel\nurl = https://ignored.test\n",
    );
    const path = join(home, "auth.toml");
    await writeFile(
      path,
      Buffer.concat([
        Buffer.from(
          '[dev]\nurl = "https://example.test"\ntoken = "SECRET-MARKER-',
        ),
        Buffer.from([0xff]),
        Buffer.from('-token"\n'),
      ]),
    );
    for (const operation of [
      () => loadProfiles(path),
      () => resolveProfile("dev", { path }),
    ]) {
      await assert.rejects(operation(), (error) => {
        assert.ok(error instanceof ProfileError);
        assert.ok(error.message.includes(JSON.stringify(path)));
        assert.ok(!error.stack.includes("SECRET-MARKER"));
        assert.ok(!error.stack.includes("ignored-ini-sentinel"));
        assert.equal(error.cause, undefined);
        return true;
      });
    }
  } finally {
    await rm(home, { recursive: true, force: true });
  }
});
