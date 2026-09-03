"""Contree configuration profiles.

The INI files under `$CONTREE_HOME`: `auth.ini`
(secrets) merged over `$CONTREE_HOME/cli.ini` (non-secret defaults),
where `CONTREE_HOME` defaults to `$XDG_CONFIG_HOME/contree` and
finally `~/.config/contree`.
"""

from __future__ import annotations

import configparser
import os
from configparser import ConfigParser
from dataclasses import dataclass, field
from pathlib import Path

from .spec_info import DEFAULT_BASE_URL

AUTH_TYPE_IAM = "iam"
AUTH_TYPE_JWT = "jwt"
PROFILE_PREFIX = "profile:"
DEFAULT_PROFILE = "default"
DEFAULT_IAM_URL = DEFAULT_BASE_URL


class ProfileError(ValueError):
    """The requested profile is missing or incomplete."""


@dataclass(frozen=True)
class Profile:
    """One Contree authentication profile.

    Frozen and comparable; the token never shows up in `repr()`.
    """

    name: str
    url: str
    token: str | None = field(repr=False)
    auth_type: str = AUTH_TYPE_JWT
    project: str | None = None

    def save(self, config: ConfigParser) -> None:
        section = f"{PROFILE_PREFIX}{self.name}"
        if not config.has_section(section):
            config.add_section(section)
        config.set(section, "url", self.url)
        config.set(section, "token", self.token or "")
        config.set(section, "type", self.auth_type)
        if self.project:
            config.set(section, "project", self.project)
        else:
            # a re-save without a project must not resurrect the old one
            config.remove_option(section, "project")


def contree_home() -> Path:
    home = os.environ.get("CONTREE_HOME")
    if home:
        return Path(home).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(xdg).expanduser() / "contree"


def load_profiles(
    path: str | Path | None = None,
) -> tuple[dict[str, Profile], str]:
    """Read all profiles; return `(profiles, active_profile_name)`.

    `cli.ini` is read first, then `auth.ini` - values from `auth.ini`
    win on conflicts.
    """
    auth_file = Path(path) if path is not None else contree_home() / "auth.ini"
    parser = configparser.ConfigParser()
    parser.read([auth_file.parent / "cli.ini", auth_file])
    active = parser.defaults().get("profile", DEFAULT_PROFILE)
    profiles: dict[str, Profile] = {}
    for section in parser.sections():
        if not section.startswith(PROFILE_PREFIX):
            continue
        name = section[len(PROFILE_PREFIX) :]
        auth_type = parser.get(section, "type", fallback=AUTH_TYPE_JWT)
        default_url = DEFAULT_IAM_URL if auth_type == AUTH_TYPE_IAM else ""
        profiles[name] = Profile(
            name=name,
            token=parser.get(section, "token", fallback=None),
            url=parser.get(section, "url", fallback=default_url).rstrip("/"),
            auth_type=auth_type,
            project=parser.get(section, "project", fallback=None),
        )
    return profiles, active


def from_env() -> Profile | None:
    """Build a profile from the environment, bypassing config files.

    Reads the standard Contree variables:
    ``CONTREE_TOKEN`` (or ``NEBIUS_API_KEY``), ``CONTREE_URL`` and
    optionally ``CONTREE_PROJECT`` (or ``NEBIUS_AI_PROJECT``).
    Reports None unless both a token and a URL are present, so callers
    can fall through to :func:`resolve_profile`.
    """
    token = os.environ.get("CONTREE_TOKEN") or os.environ.get("NEBIUS_API_KEY")
    url = os.environ.get("CONTREE_URL")
    if not token or not url:
        return None
    return Profile(
        name="environment",
        url=url.rstrip("/"),
        token=token,
        project=os.environ.get("CONTREE_PROJECT")
        or os.environ.get("NEBIUS_AI_PROJECT"),
    )


def resolve_profile(
    name: str | None = None,
    *,
    path: str | Path | None = None,
) -> Profile:
    """Resolve the active profile by name.

    Priority: explicit *name* > `CONTREE_PROFILE` environment variable
    > the active profile recorded in the config file.
    """
    profiles, active = load_profiles(path)
    selected = name or os.environ.get("CONTREE_PROFILE") or active
    if selected not in profiles:
        raise ProfileError(f"profile {selected!r} not found")
    return profiles[selected]
