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
    assert al.rules_for("click", "amazon.com", "/dp/X") == []


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
    (rule,) = al.rules_for("click", "amazon.com", "/dp/X")
    assert rule.label.search("Add to Cart")


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
    (rule,) = al.rules_for("click", "amazon.com", "/anything")
    assert rule.label.fullmatch("Place your order")


def test_second_sample_read_deny_is_valid():
    # The annotated read/deny sample must load cleanly through the schema.
    sample = SAMPLE_ALLOWLIST.parent / "allowlist-read-deny.yaml"
    al = load_allowlist(sample)
    assert al.read_policy.is_allowed("google.com", "/")  # tranco enabled in it


def test_page_scoped_sample_is_valid():
    # The page-rule walkthrough sample must load and gate per page.
    al = load_allowlist(SAMPLE_ALLOWLIST.parent / "page_scoped_write_actions.yaml")
    # read: the SPA reports page is in scope; the host is otherwise off Tranco.
    assert al.read_policy.is_allowed("app.example.com", "/", fragment="/reports/1")
    assert not al.read_policy.is_allowed("app.example.com", "/", fragment="/admin")
    # click: "add to cart" only on a product page, not in the buy pipeline.
    (dp,) = al.rules_for("click", "amazon.com", "/dp/B0X")
    assert dp.label.search("Add to Cart")
    assert al.rules_for("click", "amazon.com", "/gp/css/homepage") == []


def test_local_file_sample_opts_file_scheme_in():
    # The file:// walkthrough sample must load and actually opt the empty file
    # host in, without opening every host to non-https.
    al = load_allowlist(SAMPLE_ALLOWLIST.parent / "allow_local_file_reads.yaml")
    assert al.read_policy.override_has_host("")            # file:/// host opted in
    assert not al.read_policy.override_has_host("example.com")


def test_localhost_dev_sample_opts_localhost_in():
    # The localhost dev-server sample must load and opt localhost in (covering any
    # port) without opting 127.0.0.1 in.
    al = load_allowlist(SAMPLE_ALLOWLIST.parent / "allow_localhost_dev_server.yaml")
    assert al.read_policy.override_has_host("localhost")
    assert not al.read_policy.override_has_host("127.0.0.1")


def test_empty_host_override_loads_for_file_scheme(tmp_path):
    # "" is a valid override host (the authority-less host of file:/// URLs), so
    # an operator can scope which local-file paths reads may reach.
    f = tmp_path / "allowlist.yaml"
    f.write_text('read:\n  website_overrides:\n    "": ["^/home/me/.*"]\n')
    al = load_allowlist(f)
    assert al.read_policy.override_has_host("")


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


def test_load_page_scoped_rules(tmp_path):
    # A page-rule YAML must survive the schema-validating loader and gate
    # per-page: "place your order" only in /gp/buy/*, and a hash-router SPA read
    # override scoped by match_on: url.
    f = tmp_path / "allowlist.yaml"
    f.write_text(
        "read:\n"
        "  tranco: {enabled: true, top_n: 1000}\n"
        "  website_overrides:\n"
        "    app.example.com:\n"
        "      - path: ['^/#/reports/\\d+$']\n"
        "        match_on: url\n"
        "click:\n"
        "  amazon.com:\n"
        "    - path: ['^/(dp|gp/product)/.*']\n"
        "      label: '(?i)add to cart'\n"
        "    - path: ['^/gp/buy/.*']\n"
        "      label: '(?i)place your order'\n"
    )
    al = load_allowlist(f)
    # read override: only the reports SPA page, and it demotes the host off Tranco.
    assert al.read_policy.is_allowed("app.example.com", "/", fragment="/reports/3")
    assert not al.read_policy.is_allowed("app.example.com", "/", fragment="/admin")
    # click: label is page-scoped.
    (dp,) = al.rules_for("click", "amazon.com", "/dp/B0X")
    assert dp.label.search("Add to Cart") and not dp.label.search("Place your order")
    (buy,) = al.rules_for("click", "amazon.com", "/gp/buy/spc")
    assert buy.label.search("Place your order")
    assert al.rules_for("click", "amazon.com", "/gp/css/homepage") == []


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
    ('click:\n  amazon.com: [".*"]\n', "each page rule must be a mapping"),
    # write action, page-rule list form
    ('click:\n  amazon.com:\n    - path: ["^/dp/.*"]\n', "'label' is required"),
    ('click:\n  amazon.com:\n    - path: [".*"]\n      match_on: host\n      label: ".*"\n',
     "match_on: must be 'path' or 'url'"),
    ('click:\n  amazon.com:\n    - path: [".*"]\n      label: ".*"\n      typo: 1\n', "unknown keys"),
    ('click:\n  amazon.com: []\n', "at least one page rule"),
    # field_ids only for write-text, not click
    ('click:\n  amazon.com:\n    - path: [".*"]\n      label: ".*"\n      field_ids: [x]\n',
     "unknown keys"),
    # read override page rules may not carry a label
    ('read:\n  website_overrides:\n    x.com:\n      - path: [".*"]\n        label: ".*"\n',
     "unknown keys"),
    ('read:\n  website_overrides:\n    x.com:\n      - path: [".*"]\n        match_on: nope\n',
     "match_on: must be 'path' or 'url'"),
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
