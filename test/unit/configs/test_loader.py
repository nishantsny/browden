import pytest

from browser_guard.configs import loader as loader_pkg
from browser_guard.configs.loader import (
    ConfigError,
    SAMPLE_ALLOWLIST,
    load_allowlist,
    resolve_allowlist_path,
)


# -- load_allowlist -----------------------------------------------------------

def test_sample_config_loads_reads_open_writes_denied():
    # The shipped sample: read wide open, the add_to_cart block only present
    # as a commented-out showcase.
    al = load_allowlist(SAMPLE_ALLOWLIST)
    assert al.section("read").is_allowed("anything.example.com", "/whatever")
    assert not al.section("add_to_cart").is_allowed("amazon.com", "/dp/X")
    assert al.label_pattern("add_to_cart", "amazon.com") is None


def test_load_builds_working_allowlist(tmp_path):
    f = tmp_path / "allowlist.yaml"
    f.write_text(
        "read:\n"
        '  "*": [".*"]\n'
        "add_to_cart:\n"
        "  amazon.com:\n"
        '    paths: [".*"]\n'
        "    label: '(?i)\\badd to cart\\b'\n"
    )
    al = load_allowlist(f)
    assert al.section("add_to_cart").is_allowed("www.amazon.com", "/dp/X")
    assert al.label_pattern("add_to_cart", "amazon.com").search("Add to Cart")


def test_missing_file_raises_config_error(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_allowlist(tmp_path / "nope.yaml")


def test_invalid_yaml_raises_config_error(tmp_path):
    f = tmp_path / "allowlist.yaml"
    f.write_text("read: [unclosed\n")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_allowlist(f)


def test_empty_document_is_deny_all(tmp_path):
    f = tmp_path / "allowlist.yaml"
    f.write_text("# everything commented out\n")
    al = load_allowlist(f)
    assert not al.section("read").is_allowed("example.com", "/")


# -- schema validation --------------------------------------------------------

@pytest.mark.parametrize("content,match", [
    ("- read\n- write\n", "top level must be a mapping"),
    ("read: [1, 2]\n", "must be a mapping of host -> rule"),
    ('read:\n  "*": 42\n', "list of path regexes or a mapping"),
    ('read:\n  "*": []\n', "non-empty list"),
    ('read:\n  "*": [123]\n', "must be a string"),
    ('read:\n  "*": ["["]\n', "invalid path regex"),
    ('add_to_cart:\n  amazon.com:\n    paths: [".*"]\n    typo: x\n', "unknown keys"),
    ('add_to_cart:\n  amazon.com:\n    label: 7\n', "label: must be a regex string"),
    ('add_to_cart:\n  amazon.com:\n    label: "("\n', "label: invalid regex"),
])
def test_schema_violations_raise_config_error(tmp_path, content, match):
    f = tmp_path / "allowlist.yaml"
    f.write_text(content)
    with pytest.raises(ConfigError, match=match):
        load_allowlist(f)


# -- path resolution ----------------------------------------------------------

def test_resolve_explicit_beats_everything(monkeypatch, tmp_path):
    monkeypatch.setenv("BROWSER_GUARD_ALLOWLIST", str(tmp_path / "env.yaml"))
    assert resolve_allowlist_path(str(tmp_path / "cli.yaml")) == tmp_path / "cli.yaml"


def test_resolve_env_beats_user_config(monkeypatch, tmp_path):
    monkeypatch.setenv("BROWSER_GUARD_ALLOWLIST", str(tmp_path / "env.yaml"))
    assert resolve_allowlist_path() == tmp_path / "env.yaml"


def test_resolve_user_config_beats_sample(monkeypatch, tmp_path):
    monkeypatch.delenv("BROWSER_GUARD_ALLOWLIST", raising=False)
    monkeypatch.setattr(loader_pkg.loader, "USER_CONFIG_DIR", tmp_path)
    user = tmp_path / "allowlist.yaml"
    user.write_text("read:\n  '*': ['.*']\n")
    assert resolve_allowlist_path() == user


def test_resolve_falls_back_to_sample(monkeypatch, tmp_path):
    monkeypatch.delenv("BROWSER_GUARD_ALLOWLIST", raising=False)
    monkeypatch.setattr(loader_pkg.loader, "USER_CONFIG_DIR", tmp_path / "absent")
    assert resolve_allowlist_path() == SAMPLE_ALLOWLIST
