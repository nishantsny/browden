"""The shared profile-dir canonicalizer.

The point of the function is that two spellings of one profile reduce to one
path — so these assert equality between spellings, not a literal string.
"""
import os
from pathlib import Path

from browden.common.profile import canonical_profile_dir


def test_expands_home_to_an_absolute_path():
    out = canonical_profile_dir("~/.cache/browden/chrome-profile")
    assert out.is_absolute()
    assert out == (Path.home() / ".cache/browden/chrome-profile").resolve()


def test_spellings_of_one_profile_agree():
    home = Path.home()
    assert (canonical_profile_dir("~/.cache/browden/p")
            == canonical_profile_dir(str(home / ".cache" / "browden" / "p"))
            == canonical_profile_dir(home / ".cache/browden/./p")
            == canonical_profile_dir("~/.cache/browden/x/../p"))


def test_relative_paths_are_anchored(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert canonical_profile_dir("prof") == (tmp_path / "prof").resolve()


def test_a_profile_that_does_not_exist_yet_still_canonicalizes(tmp_path):
    # Chrome creates the directory on first launch, so a config may name a
    # profile before it exists; that must not be an error here.
    missing = tmp_path / "never-launched"
    assert not missing.exists()
    assert canonical_profile_dir(missing) == missing.resolve()


def test_it_is_idempotent(tmp_path):
    once = canonical_profile_dir(tmp_path / "p")
    assert canonical_profile_dir(once) == once


def test_the_server_resolves_profiles_through_it(tmp_path):
    # The session layer keys profiles by this path; a config scoping rules to a
    # profile has to reduce its key the same way, so both go through one
    # function. If the server stops using it, the two can drift apart.
    import browden.mcp.server as server
    assert server._resolve_profile_dir(str(tmp_path / "p")) == canonical_profile_dir(tmp_path / "p")
    assert server._resolve_profile_dir("~/x") == canonical_profile_dir("~/x")


def test_symlinked_profile_resolves_to_its_target(tmp_path):
    if os.name == "nt":  # symlinks need elevation on Windows; the CI matrix runs it
        return
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    assert canonical_profile_dir(link) == canonical_profile_dir(real)
