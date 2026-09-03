/** Contree configuration profiles.
 *
 * The INI files under `$CONTREE_HOME`: `auth.ini`
 * (secrets) merged over `$CONTREE_HOME/cli.ini` (non-secret defaults),
 * where CONTREE_HOME defaults to `$XDG_CONFIG_HOME/contree` and
 * finally `~/.config/contree`. Node-only: the filesystem modules are
 * loaded lazily so browser bundlers never pull them in.
 */

import { DEFAULT_BASE_URL } from "./specInfo.js";

export const AUTH_TYPE_IAM = "iam";
export const AUTH_TYPE_JWT = "jwt";
export const PROFILE_PREFIX = "profile:";
export const DEFAULT_PROFILE = "default";

export class ProfileError extends Error {}

function record() {
  // a null-prototype object: keys come from an external file, and a
  // key like "__proto__" must become plain data, never touch the
  // prototype chain of anything process-wide
  return Object.create(null);
}

/** A minimal INI parser covering the profile-file dialect: `[section]` headers, `key = value` pairs, `#`/`;` comments.
 * `[DEFAULT]` and keys outside any section land in the "" (defaults)
 * entry - exactly like ConfigParser's magic DEFAULT section. Returns
 * a Map of section name to a null-prototype key/value object. */
export function parseIni(text) {
  const sections = new Map([["", record()]]);
  let current = "";
  for (const rawLine of text.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#") || line.startsWith(";")) {
      continue;
    }
    if (line.startsWith("[") && line.endsWith("]")) {
      const name = line.slice(1, -1).trim();
      current = name === "DEFAULT" ? "" : name;
      if (!sections.has(current)) {
        sections.set(current, record());
      }
      continue;
    }
    const eq = line.indexOf("=");
    if (eq < 0) {
      continue;
    }
    const key = line.slice(0, eq).trim().toLowerCase();
    sections.get(current)[key] = line.slice(eq + 1).trim();
  }
  return sections;
}

async function contreeHome() {
  const { homedir } = await import("node:os");
  const { join } = await import("node:path");
  const home = process.env.CONTREE_HOME;
  if (home) {
    return home;
  }
  const xdg = process.env.XDG_CONFIG_HOME || join(homedir(), ".config");
  return join(xdg, "contree");
}

async function readIni(path) {
  const { readFile } = await import("node:fs/promises");
  try {
    return parseIni(await readFile(path, "utf-8"));
  } catch (error) {
    if (error?.code === "ENOENT") {
      return new Map([["", record()]]);
    }
    // a permission or I/O failure must not masquerade as a missing
    // profile - surface it
    throw error;
  }
}

/** Read all profiles; resolves to `{profiles, active}`.
 *
 * `cli.ini` is read first, then `auth.ini` - values from `auth.ini`
 * win on conflicts. `[DEFAULT]` values are inherited by every profile
 * section (ConfigParser semantics, like the Python client).
 */
export async function loadProfiles(path = null) {
  const { dirname, join } = await import("node:path");
  const authFile = path ?? join(await contreeHome(), "auth.ini");
  const merged = new Map([["", record()]]);
  for (const file of [join(dirname(authFile), "cli.ini"), authFile]) {
    for (const [section, values] of await readIni(file)) {
      const target = merged.get(section) ?? record();
      Object.assign(target, values);
      merged.set(section, target);
    }
  }
  const defaults = merged.get("");
  const active = defaults.profile ?? DEFAULT_PROFILE;
  const profiles = record();
  for (const [section, values] of merged) {
    if (!section.startsWith(PROFILE_PREFIX)) {
      continue;
    }
    const name = section.slice(PROFILE_PREFIX.length);
    // ConfigParser exposes DEFAULT values through every section
    const effective = record();
    Object.assign(effective, defaults, values);
    const authType = effective.type ?? AUTH_TYPE_JWT;
    const defaultUrl = authType === AUTH_TYPE_IAM ? DEFAULT_BASE_URL : "";
    profiles[name] = {
      name,
      token: effective.token ?? null,
      url: (effective.url ?? defaultUrl).replace(/\/+$/, ""),
      authType,
      project: effective.project ?? null,
    };
  }
  return { profiles, active };
}

/** Build a profile from the environment, bypassing config files.
 *
 * Reads the standard Contree variables: `CONTREE_TOKEN`
 * (or `NEBIUS_API_KEY`), `CONTREE_URL` and optionally
 * `CONTREE_PROJECT` (or `NEBIUS_AI_PROJECT`). Returns null unless
 * both a token and a URL are present - a non-null result means the
 * environment fully described a profile.
 */
export function fromEnv() {
  const env = globalThis.process?.env ?? {};
  const token = env.CONTREE_TOKEN || env.NEBIUS_API_KEY || null;
  const url = env.CONTREE_URL || null;
  if (!token || !url) {
    return null;
  }
  return {
    name: "environment",
    token,
    url: url.replace(/\/+$/, ""),
    authType: AUTH_TYPE_JWT,
    project: env.CONTREE_PROJECT ?? env.NEBIUS_AI_PROJECT ?? null,
  };
}

/** Resolve the active profile by name.
 *
 * Priority: explicit *name* > `CONTREE_PROFILE` environment variable
 * > the active profile recorded in the config file (`[DEFAULT]`).
 */
export async function resolveProfile(name = null, { path = null } = {}) {
  const { profiles, active } = await loadProfiles(path);
  const selected = name || process.env.CONTREE_PROFILE || active;
  if (!(selected in profiles)) {
    throw new ProfileError(`profile ${JSON.stringify(selected)} not found`);
  }
  return profiles[selected];
}
