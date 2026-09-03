export declare const AUTH_TYPE_IAM: "iam";
export declare const AUTH_TYPE_JWT: "jwt";
export declare const PROFILE_PREFIX: "profile:";
export declare const DEFAULT_PROFILE: "default";

export declare class ProfileError extends Error {}

export interface Profile {
  name: string;
  url: string;
  token: string | null;
  authType: string;
  project: string | null;
}

export declare function parseIni(
  text: string,
): Map<string, Record<string, string>>;

export declare function fromEnv(): Profile | null;

export declare function loadProfiles(
  path?: string | null,
): Promise<{ profiles: Record<string, Profile>; active: string }>;

export declare function resolveProfile(
  name?: string | null,
  options?: { path?: string | null },
): Promise<Profile>;
