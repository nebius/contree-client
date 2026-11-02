"""Public saved-profile TOML regressions."""

from __future__ import annotations

import traceback
from pathlib import Path
from types import ModuleType

import pytest


def test_toml_role_preference_and_partial_merge(
    profiles: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CONTREE_HOME", str(tmp_path))
    monkeypatch.delenv("CONTREE_PROFILE", raising=False)
    (tmp_path / "cli.ini").write_text(
        "[DEFAULT]\nprofile = dev\n[profile:dev]\nurl = https://cli.example/\n"
        "project = cli-project\n"
    )
    (tmp_path / "auth.ini").write_text(
        "[profile:ignored]\ntoken = ignored-token\nurl = https://ignored.example\n"
    )
    (tmp_path / "auth.toml").write_text('[dev]\ntoken = "dummy%token"\n[partial]\n')
    loaded, active = profiles.load_profiles()
    assert active == "dev"
    assert set(loaded) == {"dev", "partial"}
    selected = profiles.resolve_profile()
    assert (selected.url, selected.token, selected.project) == (
        "https://cli.example",
        "dummy%token",
        "cli-project",
    )
    with pytest.raises(profiles.ProfileError, match="token"):
        profiles.resolve_profile("partial")


@pytest.mark.parametrize("invalid", ["token = 42", 'token = "SECRET-MARKER" broken'])
def test_toml_errors_are_safe_without_fallback(
    profiles: ModuleType, tmp_path: Path, invalid: str
) -> None:
    (tmp_path / "auth.ini").write_text(
        "[profile:dev]\ntoken = ignored-token\nurl = https://ignored.example\n"
    )
    path = tmp_path / "auth.toml"
    path.write_text('[dev]\nurl = "https://example.test"\n' + invalid)
    with pytest.raises(profiles.ProfileError) as caught:
        profiles.load_profiles(path)
    rendered = "".join(traceback.format_exception(caught.value))
    assert "SECRET-MARKER" not in rendered
    assert "ignored-token" not in rendered
    assert str(path) in str(caught.value)


def test_explicit_toml_missing_is_authoritative(
    profiles: ModuleType, tmp_path: Path
) -> None:
    (tmp_path / "cli.toml").write_text(
        '[dev]\nurl = "https://example.test"\ntoken = "dummy"\n'
    )
    with pytest.raises(profiles.ProfileError, match=r"missing\.toml"):
        profiles.resolve_profile("dev", path=tmp_path / "missing.toml")
