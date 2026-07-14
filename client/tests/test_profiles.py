from __future__ import annotations

import configparser
import importlib
from pathlib import Path
from types import ModuleType

import pytest

AUTH_INI = """
[DEFAULT]
profile = default

[profile:default]
token = secret-tok
url = https://api.tokenfactory.nebius.com/sandboxes
type = iam
project = aiproject-x

[profile:staging]
token = tok2
url = https://staging.dev
type = iam
project = staging-project

[profile:legacy]
token = tok3
url = https://legacy.dev
"""


@pytest.fixture
def config_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    monkeypatch.setenv("CONTREE_HOME", str(tmp_path))
    monkeypatch.delenv("CONTREE_PROFILE", raising=False)
    (tmp_path / "auth.ini").write_text(AUTH_INI)
    return tmp_path


def test_load_profiles(profiles: ModuleType, config_home: Path) -> None:
    loaded, active = profiles.load_profiles()
    assert active == "default"
    assert set(loaded) == {"default", "staging", "legacy"}
    default = loaded["default"]
    assert default.token == "secret-tok"
    assert default.project == "aiproject-x"
    assert default.auth_type == "iam"


def test_legacy_profile_defaults_to_jwt(
    profiles: ModuleType,
    config_home: Path,
) -> None:
    loaded, _ = profiles.load_profiles()
    assert loaded["legacy"].auth_type == "jwt"
    assert loaded["legacy"].project is None


def test_iam_profile_default_url(
    profiles: ModuleType,
    config_home: Path,
) -> None:
    (config_home / "auth.ini").write_text("[profile:default]\ntoken = t\ntype = iam\n")
    loaded, _ = profiles.load_profiles()
    assert loaded["default"].url == profiles.DEFAULT_IAM_URL


def test_cli_ini_merge_auth_wins(
    profiles: ModuleType,
    config_home: Path,
) -> None:
    (config_home / "cli.ini").write_text(
        "[profile:default]\nurl = https://cli.dev\nproject = from-cli\n"
        "[profile:extra]\nurl = https://extra.dev\ntype = jwt\n"
    )
    (config_home / "auth.ini").write_text(
        "[profile:default]\ntoken = t\nurl = https://auth.dev\ntype = iam\n"
    )
    loaded, _ = profiles.load_profiles()
    assert loaded["default"].url == "https://auth.dev"
    assert loaded["default"].project == "from-cli"
    assert loaded["extra"].url == "https://extra.dev"


def test_resolve_priority_env(
    profiles: ModuleType,
    config_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CONTREE_PROFILE", "staging")
    assert profiles.resolve_profile().name == "staging"
    assert profiles.resolve_profile("legacy").name == "legacy"


def test_resolve_missing_profile_raises(
    profiles: ModuleType,
    config_home: Path,
) -> None:
    with pytest.raises(profiles.ProfileError, match="nope"):
        profiles.resolve_profile("nope")


def test_repr_hides_token(profiles: ModuleType, config_home: Path) -> None:
    loaded, _ = profiles.load_profiles()
    rendered = repr(loaded["default"])
    assert "secret-tok" not in rendered
    assert "name='default'" in rendered
    assert "project='aiproject-x'" in rendered


def test_profiles_comparable(profiles: ModuleType, config_home: Path) -> None:
    first, _ = profiles.load_profiles()
    second, _ = profiles.load_profiles()
    assert first["default"] == second["default"]
    assert first["default"] != first["staging"]


def test_profile_save_writes_all_fields(profiles: ModuleType) -> None:
    parser = configparser.ConfigParser()
    profile = profiles.Profile(
        name="new",
        url="https://x.dev",
        token="tok",
        auth_type="iam",
        project="proj",
    )
    profile.save(parser)
    section = "profile:new"
    assert parser.get(section, "url") == "https://x.dev"
    assert parser.get(section, "token") == "tok"
    assert parser.get(section, "type") == "iam"
    assert parser.get(section, "project") == "proj"


def test_profile_save_minimal(profiles: ModuleType) -> None:
    parser = configparser.ConfigParser()
    profiles.Profile(name="p", url="https://x.dev", token=None).save(parser)
    section = "profile:p"
    assert parser.get(section, "token") == ""
    assert parser.get(section, "type") == profiles.AUTH_TYPE_JWT
    assert not parser.has_option(section, "project")


def test_profile_save_updates_existing_section(profiles: ModuleType) -> None:
    parser = configparser.ConfigParser()
    profiles.Profile(name="p", url="https://old.dev", token="t1").save(parser)
    profiles.Profile(name="p", url="https://new.dev", token="t2").save(parser)
    assert parser.sections() == ["profile:p"]
    assert parser.get("profile:p", "url") == "https://new.dev"
    assert parser.get("profile:p", "token") == "t2"


def test_profile_save_load_roundtrip(
    profiles: ModuleType,
    config_home: Path,
) -> None:
    profile = profiles.Profile(
        name="roundtrip",
        url="https://rt.dev",
        token="rt-token",
        auth_type="iam",
        project="rt-project",
    )
    parser = configparser.ConfigParser()
    profile.save(parser)
    with (config_home / "auth.ini").open("w") as fp:
        parser.write(fp)
    loaded, _ = profiles.load_profiles()
    assert loaded["roundtrip"] == profile


def test_contree_home_resolution(
    profiles: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("CONTREE_HOME", str(tmp_path / "custom"))
    assert profiles.contree_home() == tmp_path / "custom"
    monkeypatch.delenv("CONTREE_HOME")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert profiles.contree_home() == tmp_path / "xdg" / "contree"
    monkeypatch.delenv("XDG_CONFIG_HOME")
    assert profiles.contree_home() == Path("~/.config").expanduser() / "contree"


def test_client_from_profile(
    generated_package: ModuleType,
    profiles: ModuleType,
    config_home: Path,
) -> None:
    module = importlib.import_module("contree_client.requests")
    client = module.ContreeClient.from_profile("staging")
    try:
        assert client.token == "tok2"
        assert client.base_url == "https://staging.dev"
        assert client.project == "staging-project"
    finally:
        client.close()


def test_client_from_profile_instance(
    generated_package: ModuleType,
    profiles: ModuleType,
) -> None:
    profile = profiles.Profile(
        name="inline",
        url="https://inline.dev",
        token="tok",
        auth_type="iam",
        project="proj",
    )
    module = importlib.import_module("contree_client.requests")
    with module.ContreeClient.from_profile(profile) as client:
        assert client.token == "tok"
        assert client.base_url == "https://inline.dev"
        assert client.project == "proj"


def test_client_from_profile_without_token(
    generated_package: ModuleType,
    profiles: ModuleType,
    config_home: Path,
) -> None:
    (config_home / "auth.ini").write_text(
        "[profile:default]\nurl = https://x.dev\ntype = jwt\n"
    )
    module = importlib.import_module("contree_client.requests")
    with pytest.raises(profiles.ProfileError, match="token"):
        module.ContreeClient.from_profile()


def test_from_profile_rejects_blank_token(
    generated_package: ModuleType, profiles: ModuleType
) -> None:
    """P2-19: an empty token must not become `Authorization: Bearer `."""
    testing = importlib.import_module("contree_client.testing")
    blank = profiles.Profile(
        name="blank", token="   ", url="https://contree.example.com"
    )
    with pytest.raises(profiles.ProfileError, match="token"):
        testing.ContreeClient.from_profile(blank)


def test_profile_save_removes_stale_project(profiles: ModuleType) -> None:
    """P2-19: re-saving without a project must drop the old value."""
    config = configparser.ConfigParser()
    with_project = profiles.Profile(
        name="p", token="t", url="https://x.example", project="old-project"
    )
    with_project.save(config)
    assert config.get("profile:p", "project") == "old-project"

    without_project = profiles.Profile(
        name="p", token="t", url="https://x.example", project=None
    )
    without_project.save(config)
    assert not config.has_option("profile:p", "project")


def test_from_env_builds_a_profile(
    generated_package: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CONTREE_TOKEN/CONTREE_URL (the standard Contree names) fully
    describe a profile; a non-None result means it parsed."""
    profiles = importlib.import_module("contree_client.profiles")

    for name in (
        "CONTREE_TOKEN",
        "NEBIUS_API_KEY",
        "CONTREE_URL",
        "CONTREE_PROJECT",
        "NEBIUS_AI_PROJECT",
    ):
        monkeypatch.delenv(name, raising=False)
    assert profiles.from_env() is None

    monkeypatch.setenv("CONTREE_TOKEN", "env-token")
    assert profiles.from_env() is None  # a token alone is not enough

    monkeypatch.setenv("CONTREE_URL", "https://env.example.com/")
    profile = profiles.from_env()
    assert profile is not None
    assert profile.token == "env-token"
    assert profile.url == "https://env.example.com"  # trailing / stripped
    assert profile.project is None

    monkeypatch.setenv("CONTREE_PROJECT", "env-project")
    assert profiles.from_env().project == "env-project"

    # the NEBIUS_* fallbacks kick in when the CONTREE_* names are unset
    monkeypatch.delenv("CONTREE_TOKEN")
    monkeypatch.delenv("CONTREE_PROJECT")
    monkeypatch.setenv("NEBIUS_API_KEY", "nebius-token")
    monkeypatch.setenv("NEBIUS_AI_PROJECT", "nebius-project")
    profile = profiles.from_env()
    assert profile.token == "nebius-token"
    assert profile.project == "nebius-project"
