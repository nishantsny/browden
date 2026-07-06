import pytest

from browden.configs import loader as loader_pkg
from browden.configs.loader import (
    ConfigError,
    SAMPLE_ALLOWLIST,
    load_allowlist,
    resolve_allowlist_path,
)


# -- load_allowlist -----------------------------------------------------------

def test_sample_config_gates_reads_by_tranco_and_denies_writes():
    # The shipped sample: reads gated by Tranco top-sites, denylist empty, the
    # click block only present as a commented-out showcase.
    al = load_allowlist(SAMPLE_ALLOWLIST)
    assert al.read_policy.is_allowed("google.com", "/")            # top site -> allowed
    assert not al.read_policy.is_allowed("nonexistent-xyz-9876.test", "/")  # not a top site
    assert not al.is_denied("google.com", "/")                     # denylist empty
    assert not al.section("click").is_allowed("amazon.com", "/dp/X")
    assert al.label_pattern("click", "amazon.com") is None


def test_load_builds_working_allowlist(tmp_path):
    f = tmp_path / "allowlist.yaml"
    f.write_text(
        "read:\n"
        "  tranco: {enabled: true, top_n: 1000}\n"
        "  website_overrides:\n"
        '    "*": [".*"]\n'
        "denylist:\n"
        '  blocked.test: [".*"]\n'
        "click:\n"
        "  amazon.com:\n"
        '    paths: [".*"]\n'
        "    label: '(?i)\\badd to cart\\b'\n"
    )
    al = load_allowlist(f)
    assert al.read_policy.is_allowed("anything.test", "/")     # overrides "*"
    assert not al.read_policy.is_allowed("blocked.test", "/")  # denylist wins
    assert al.section("click").is_allowed("www.amazon.com", "/dp/X")
    assert al.label_pattern("click", "amazon.com").search("Add to Cart")


def test_explicit_wildcard_label_allows_any_control(tmp_path):
    # "Allow any control on this host" is stated explicitly as label: '.*'.
    f = tmp_path / "allowlist.yaml"
    f.write_text(
        "read:\n"
        '  website_overrides: {"*": [".*"]}\n'
        "click:\n"
        "  amazon.com:\n"
        "    label: '.*'\n"
    )
    al = load_allowlist(f)
    assert al.section("click").is_allowed("amazon.com", "/anything")
    assert al.label_pattern("click", "amazon.com").fullmatch("Place your order")


def test_second_sample_read_deny_is_valid():
    # The annotated read/deny sample must load cleanly through the schema.
    sample = SAMPLE_ALLOWLIST.parent / "allowlist-read-deny.yaml"
    al = load_allowlist(sample)
    assert al.read_policy.is_allowed("google.com", "/")  # tranco enabled in it


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
    assert not al.read_policy.is_allowed("example.com", "/")


# -- schema validation --------------------------------------------------------

@pytest.mark.parametrize("content,match", [
    ("- read\n- write\n", "top level must be a mapping"),
    # read block
    ("read: [1, 2]\n", "read: must be a mapping"),
    ("read:\n  bogus: 1\n", "read: unknown keys"),
    ("read:\n  enabled: 3\n", "enabled: must be a boolean"),
    ("read:\n  tranco: 5\n", "tranco: must be a mapping"),
    ("read:\n  tranco:\n    bogus: 1\n", "tranco: unknown keys"),
    ("read:\n  tranco:\n    top_n: -1\n", "top_n: must be a positive integer"),
    ("read:\n  tranco:\n    top_n: true\n", "top_n: must be a positive integer"),
    ('read:\n  website_overrides:\n    "*": []\n', "non-empty list"),
    ('read:\n  website_overrides:\n    "*": [123]\n', "must be a string"),
    ('read:\n  website_overrides:\n    "*": ["["]\n', "invalid path regex"),
    # denylist block
    ("denylist: 5\n", "denylist: expected a mapping"),
    ('denylist:\n  "*": []\n', "non-empty list"),
    # write action
    ('click:\n  amazon.com:\n    paths: [".*"]\n    typo: x\n', "unknown keys"),
    ('click:\n  amazon.com:\n    label: 7\n', "label: must be a regex string"),
    ('click:\n  amazon.com:\n    label: "("\n', "label: invalid regex"),
    # label is required — "allow any control" must be explicit as label: '.*'
    ('click:\n  amazon.com:\n    paths: [".*"]\n', "'label' is required"),
    ('click:\n  amazon.com: {}\n', "'label' is required"),
    ('click:\n  amazon.com: [".*"]\n', "must be a mapping with a required 'label'"),
    # infra
    ("infra:\n  max_tabs_per_session: -1\n", "must be a positive integer"),
])
def test_schema_violations_raise_config_error(tmp_path, content, match):
    f = tmp_path / "allowlist.yaml"
    f.write_text(content)
    with pytest.raises(ConfigError, match=match):
        load_allowlist(f)


# -- path resolution ----------------------------------------------------------

def test_resolve_explicit_beats_everything(monkeypatch, tmp_path):
    monkeypatch.setenv("BROWDEN_ALLOWLIST", str(tmp_path / "env.yaml"))
    assert resolve_allowlist_path(str(tmp_path / "cli.yaml")) == tmp_path / "cli.yaml"


def test_resolve_env_beats_user_config(monkeypatch, tmp_path):
    monkeypatch.setenv("BROWDEN_ALLOWLIST", str(tmp_path / "env.yaml"))
    assert resolve_allowlist_path() == tmp_path / "env.yaml"


def test_resolve_user_config_beats_sample(monkeypatch, tmp_path):
    monkeypatch.delenv("BROWDEN_ALLOWLIST", raising=False)
    monkeypatch.setattr(loader_pkg.loader, "USER_CONFIG_DIR", tmp_path)
    user = tmp_path / "allowlist.yaml"
    user.write_text("read:\n  enabled: false\n")
    assert resolve_allowlist_path() == user


def test_resolve_falls_back_to_sample(monkeypatch, tmp_path):
    monkeypatch.delenv("BROWDEN_ALLOWLIST", raising=False)
    monkeypatch.setattr(loader_pkg.loader, "USER_CONFIG_DIR", tmp_path / "absent")
    assert resolve_allowlist_path() == SAMPLE_ALLOWLIST
