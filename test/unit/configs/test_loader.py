from pathlib import Path

import pytest

from safe_agent_browser.configs import loader as loader_pkg
from safe_agent_browser.configs.loader import (
    ConfigError,
    SAMPLE_ALLOWLIST,
    load_runtime_configuration,
    resolve_allowlist_path,
)
from safe_agent_browser.mcp.validator.allowlist import WRITE_ACTIONS


# -- load_runtime_configuration -----------------------------------------------------------

def test_sample_config_gates_reads_by_tranco_and_denies_writes():
    # The shipped sample: reads gated by Tranco top-sites, denylist empty, the
    # click block only present as a commented-out showcase.
    rc = load_runtime_configuration(SAMPLE_ALLOWLIST)
    assert rc.access_rules.read_policy.is_allowed("google.com", "/")            # top site -> allowed
    assert not rc.access_rules.read_policy.is_allowed("nonexistent-xyz-9876.test", "/")  # not a top site
    assert not rc.access_rules.is_denied("google.com", "/")                     # denylist empty
    assert not rc.access_rules.section("click").is_allowed("amazon.com", "/dp/X")
    assert rc.access_rules.rules_for("click", "amazon.com", "/dp/X") == []


def test_load_builds_working_runtime_configuration(tmp_path):
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
    rc = load_runtime_configuration(f)
    assert rc.access_rules.read_policy.is_allowed("anything.test", "/")     # overrides "*"
    assert not rc.access_rules.read_policy.is_allowed("blocked.test", "/")  # denylist wins
    assert rc.access_rules.section("click").is_allowed("www.amazon.com", "/dp/X")
    (rule,) = rc.access_rules.rules_for("click", "amazon.com", "/dp/X")
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
    rc = load_runtime_configuration(f)
    assert rc.access_rules.section("click").is_allowed("amazon.com", "/anything")
    (rule,) = rc.access_rules.rules_for("click", "amazon.com", "/anything")
    assert rule.label.fullmatch("Place your order")


def test_second_sample_read_deny_is_valid():
    # The annotated read/deny sample must load cleanly through the schema.
    sample = SAMPLE_ALLOWLIST.parent / "allowlist-read-deny.yaml"
    rc = load_runtime_configuration(sample)
    assert rc.access_rules.read_policy.is_allowed("google.com", "/")  # tranco enabled in it


def test_page_scoped_sample_is_valid():
    # The page-rule walkthrough sample must load and gate per page.
    rc = load_runtime_configuration(SAMPLE_ALLOWLIST.parent / "page_scoped_write_actions.yaml")
    # read: the SPA reports page is in scope; the host is otherwise off Tranco.
    assert rc.access_rules.read_policy.is_allowed("app.example.com", "/", fragment="/reports/1")
    assert not rc.access_rules.read_policy.is_allowed("app.example.com", "/", fragment="/admin")
    # click: "add to cart" only on a product page, not in the buy pipeline.
    (dp,) = rc.access_rules.rules_for("click", "amazon.com", "/dp/B0X")
    assert dp.label.search("Add to Cart")
    assert rc.access_rules.rules_for("click", "amazon.com", "/gp/css/homepage") == []


def test_local_file_sample_opts_file_scheme_in():
    # The file:// walkthrough sample must load and actually opt the empty file
    # host in, without opening every host to non-https.
    rc = load_runtime_configuration(SAMPLE_ALLOWLIST.parent / "allow_local_file_reads.yaml")
    assert rc.access_rules.read_policy.override_has_host("")            # file:/// host opted in
    assert not rc.access_rules.read_policy.override_has_host("example.com")


def test_localhost_dev_sample_opts_localhost_in():
    # The localhost dev-server sample must load and opt localhost in (covering any
    # port) without opting 127.0.0.1 in.
    rc = load_runtime_configuration(SAMPLE_ALLOWLIST.parent / "allow_localhost_dev_server.yaml")
    assert rc.access_rules.read_policy.override_has_host("localhost")
    assert not rc.access_rules.read_policy.override_has_host("127.0.0.1")


def test_press_key_sample_is_valid():
    # The press-key walkthrough sample must load through the schema — its rules
    # carry a `keys` field, which the loader's schema must accept (regression:
    # `keys` was added to the runtime parser but not the file-load schema, so the
    # sample booted fine in dict tests yet the server refused it on load).
    rc = load_runtime_configuration(SAMPLE_ALLOWLIST.parent / "allow_press_key_activation.yaml")
    (rule,) = rc.access_rules.rules_for("press-key", "cronometer.com", "/")
    assert "Enter" in rule.keys


def test_load_press_key_rule_keys(tmp_path):
    # The full file path: schema-validate a press-key rule and parse its `keys`
    # into PageRule.keys.
    f = tmp_path / "allowlist.yaml"
    f.write_text(
        'read:\n  website_overrides: {"*": [".*"]}\n'
        "press-key:\n"
        "  cronometer.com:\n"
        "    - path: ['^/$']\n"
        "      label: '.*'\n"
        "      keys: ['Enter', 'ArrowDown']\n"
    )
    rc = load_runtime_configuration(f)
    (rule,) = rc.access_rules.rules_for("press-key", "cronometer.com", "/")
    assert rule.keys == frozenset({"Enter", "ArrowDown"})


@pytest.mark.parametrize("action", WRITE_ACTIONS)
def test_every_write_action_section_loads(tmp_path, action):
    # The schema refuses unknown top-level sections, so each gated write action
    # must stay accepted by name.
    f = tmp_path / "allowlist.yaml"
    f.write_text(f'{action}:\n  x.com:\n    label: ".*"\n')
    rc = load_runtime_configuration(f)
    assert rc.access_rules.rules_for(action, "x.com", "/")


def test_empty_host_override_loads_for_file_scheme(tmp_path):
    # "" is a valid override host (the authority-less host of file:/// URLs), so
    # an operator can scope which local-file paths reads may reach.
    f = tmp_path / "allowlist.yaml"
    f.write_text('read:\n  website_overrides:\n    "": ["^/home/me/.*"]\n')
    rc = load_runtime_configuration(f)
    assert rc.access_rules.read_policy.override_has_host("")


def test_missing_file_raises_config_error(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_runtime_configuration(tmp_path / "nope.yaml")


def test_invalid_yaml_raises_config_error(tmp_path):
    f = tmp_path / "allowlist.yaml"
    f.write_text("read: [unclosed\n")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_runtime_configuration(f)


def test_empty_document_is_deny_all(tmp_path):
    f = tmp_path / "allowlist.yaml"
    f.write_text("# everything commented out\n")
    rc = load_runtime_configuration(f)
    assert not rc.access_rules.read_policy.is_allowed("example.com", "/")


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
    rc = load_runtime_configuration(f)
    # read override: only the reports SPA page, and it demotes the host off Tranco.
    assert rc.access_rules.read_policy.is_allowed("app.example.com", "/", fragment="/reports/3")
    assert not rc.access_rules.read_policy.is_allowed("app.example.com", "/", fragment="/admin")
    # click: label is page-scoped.
    (dp,) = rc.access_rules.rules_for("click", "amazon.com", "/dp/B0X")
    assert dp.label.search("Add to Cart") and not dp.label.search("Place your order")
    (buy,) = rc.access_rules.rules_for("click", "amazon.com", "/gp/buy/spc")
    assert buy.label.search("Place your order")
    assert rc.access_rules.rules_for("click", "amazon.com", "/gp/css/homepage") == []


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
    # top-level sections: only the known write actions + denylist/read/infra
    ('clik:\n  amazon.com:\n    label: ".*"\n', "unknown section 'clik'"),
    ('insert_text:\n  amazon.com:\n    label: ".*"\n', "unknown section 'insert_text'"),
    ("5: {}\n", "unknown section 5"),
    ("bogus: {}\n", r"allowed: allowed_upload_locations, click, denylist, infra, press-key, profiles, read, upload-file, write-text\)"),
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
    # keys only for press-key, not click
    ('click:\n  amazon.com:\n    - path: [".*"]\n      label: ".*"\n      keys: [Enter]\n',
     "unknown keys"),
    # press-key keys must be a non-empty list of strings
    ('press-key:\n  x.com:\n    - path: [".*"]\n      label: ".*"\n      keys: []\n',
     "non-empty list"),
    ('press-key:\n  x.com:\n    - path: [".*"]\n      label: ".*"\n      keys: "Enter"\n',
     "non-empty list"),
    # read override page rules may not carry a label
    ('read:\n  website_overrides:\n    x.com:\n      - path: [".*"]\n        label: ".*"\n',
     "unknown keys"),
    ('read:\n  website_overrides:\n    x.com:\n      - path: [".*"]\n        match_on: nope\n',
     "match_on: must be 'path' or 'url'"),
    # profiles block. The keys are spelled `~/p`: it expands to an absolute path
    # on every platform, where a literal `/tmp/p` is absolute only on POSIX (on
    # Windows it has no drive, so the absolute-key check would fire first and
    # these cases would never reach the error each is about).
    ("profiles: 5\n", "profiles must be a mapping"),
    ("profiles:\n  relative/profile: {}\n", "must be an absolute profile directory"),
    ("profiles:\n  ~/p:\n    infra: {max_tabs_per_session: 2}\n",
     "'infra' is not allowed inside a profile"),
    ("profiles:\n  ~/p:\n    profiles: {}\n",
     "'profiles' is not allowed inside a profile"),
    ("profiles:\n  ~/p: 5\n", "must be a mapping of rules"),
    # a profile body is validated by the same rules as a top-level one
    ("profiles:\n  ~/p:\n    click:\n      amazon.com:\n        paths: ['.*']\n",
     "'label' is required"),
    ("profiles:\n  ~/p:\n    read:\n      website_overrides:\n        x.com: ['[']\n",
     "invalid path regex"),
    ("profiles:\n  ~/p:\n    read:\n      typo: 1\n", "unknown keys"),
    # a typo'd section inside a profile fails the load, naming the profile, rather
    # than loading as rules no gate consults
    ("profiles:\n  ~/p:\n    clik:\n      amazon.com:\n        label: '.*'\n",
     r"profiles\.~/p: unknown section 'clik'"),
    # allow_all is a profile-scoped flag, not a global one
    ("allow_all: true\n", "only allowed inside a profiles entry"),
    ("profiles:\n  ~/p:\n    allow_all: sure\n", "allow_all: must be a boolean"),
    # allowed_upload_locations: the filesystem bound on upload-file
    ("allowed_upload_locations: '~/receipts'\n", "must be a list of directory paths"),
    ("allowed_upload_locations: [5]\n", "must be a list of directory paths"),
    ("allowed_upload_locations: ['']\n", "must be a list of directory paths"),
    ("allowed_upload_locations: ['relative/receipts']\n", "must be an absolute directory"),
    # field_ids IS legal under upload-file (a file input rarely has a visible
    # label), but `keys` is still press-key's alone
    ('upload-file:\n  x.com:\n    - path: [".*"]\n      label: ".*"\n      keys: [Enter]\n',
     "unknown keys"),
    ('upload-file:\n  x.com:\n    - path: [".*"]\n', "'label' is required"),
    # infra
    ("infra:\n  max_tabs_per_session: -1\n", "must be a positive integer"),
    ("infra:\n  reap_interval_seconds: 0\n", "must be a positive integer"),
    ("infra:\n  reap_interval_secs: 60\n", "unknown infra key"),
])
def test_schema_violations_raise_config_error(tmp_path, content, match):
    f = tmp_path / "allowlist.yaml"
    f.write_text(content)
    with pytest.raises(ConfigError, match=match):
        load_runtime_configuration(f)


def test_upload_file_rules_and_roots_load(tmp_path):
    """The shape the Splitwise case needs: field_ids on upload-file, plus a location."""
    f = tmp_path / "allowlist.yaml"
    f.write_text(
        "allowed_upload_locations:\n"
        "  - ~/receipts\n"
        "upload-file:\n"
        "  secure.splitwise.com:\n"
        "    - path: ['^/$', '^/#/.*']\n"
        "      match_on: url\n"
        "      label: '(?i)attach an image or pdf:?'\n"
        "      field_ids: [bill_file_expense]\n")
    rules = load_runtime_configuration(f).access_rules
    assert rules.allowed_upload_locations == (Path("~/receipts").expanduser().resolve(),)
    matched = rules.rules_for("upload-file", "secure.splitwise.com", "/", "", "/expenses")
    assert [sorted(r.field_ids) for r in matched] == [["bill_file_expense"]]


def test_no_allowed_upload_locations_means_no_upload_is_authorized(tmp_path):
    f = tmp_path / "allowlist.yaml"
    f.write_text("upload-file:\n  x.com:\n    - path: ['.*']\n      label: '.*'\n")
    assert load_runtime_configuration(f).access_rules.allowed_upload_locations == ()


def test_a_profile_adds_allowed_upload_locations_over_the_global_ones(tmp_path):
    f = tmp_path / "allowlist.yaml"
    f.write_text(
        "allowed_upload_locations: ['~/receipts']\n"
        "profiles:\n"
        "  ~/p:\n"
        "    allowed_upload_locations: ['~/invoices']\n")
    configuration = load_runtime_configuration(f)
    scoped = configuration.access_rules_for(Path("~/p").expanduser())
    assert set(scoped.allowed_upload_locations) == {Path("~/receipts").expanduser().resolve(),
                                        Path("~/invoices").expanduser().resolve()}
    # The global set is untouched by what a profile added.
    assert configuration.access_rules.allowed_upload_locations == (Path("~/receipts").expanduser().resolve(),)


def test_load_profile_scoped_rules(tmp_path):
    # The full path a live server takes: YAML -> schema -> SafeAgentBrowserRuntimeConfiguration ->
    # a decision scoped to the profile the request runs in.
    shopper = tmp_path / "shopper"
    reader = tmp_path / "reader"
    f = tmp_path / "allowlist.yaml"
    f.write_text(
        "read:\n"
        "  tranco: {enabled: false}\n"
        "  website_overrides:\n"
        "    everyone.test: ['.*']\n"
        "profiles:\n"
        f"  {shopper}:\n"
        "    click:\n"
        "      shop.test:\n"
        "        - path: ['^/cart.*']\n"
        "          label: '(?i)add to cart'\n"
        f"  {reader}:\n"
        "    read:\n"
        "      website_overrides:\n"
        "        docs.test: ['^/pages/.*']\n"
    )
    rc = load_runtime_configuration(f)
    assert rc.profile_dirs() == sorted([str(shopper), str(reader)])

    shop_policy = rc.access_rules_for(str(shopper))
    read_policy = rc.access_rules_for(str(reader))
    # Each profile has its own grant...
    assert shop_policy.rules_for("click", "shop.test", "/cart")
    assert read_policy.read_policy.is_allowed("docs.test", "/pages/x")
    # ...and not the other's.
    assert read_policy.rules_for("click", "shop.test", "/cart") == []
    assert not shop_policy.read_policy.is_allowed("docs.test", "/pages/x")
    # ...on top of the global one, which both keep.
    for policy in (shop_policy, read_policy, rc.access_rules):
        assert policy.read_policy.is_allowed("everyone.test", "/")


def test_profile_key_with_a_tilde_resolves_to_the_home_path(tmp_path):
    f = tmp_path / "allowlist.yaml"
    f.write_text(
        "profiles:\n"
        "  ~/.cache/safe-agent-browser/scratch:\n"
        "    read:\n"
        "      website_overrides:\n"
        "        ok.test: ['.*']\n"
    )
    rc = load_runtime_configuration(f)
    from pathlib import Path as _Path
    resolved = str((_Path.home() / ".cache/safe-agent-browser/scratch").resolve())
    assert rc.profile_dirs() == [resolved]
    assert rc.access_rules_for(resolved).read_policy.is_allowed("ok.test", "/")


def test_a_profile_that_does_not_exist_yet_loads(tmp_path):
    # Chrome creates a profile directory on first launch, so a config naming a
    # profile before it has ever been used must load — that is exactly the
    # "scope down to a fresh profile" case the block exists for.
    f = tmp_path / "allowlist.yaml"
    f.write_text(
        "profiles:\n"
        f"  {tmp_path / 'never-launched'}:\n"
        "    read:\n"
        "      website_overrides:\n"
        "        ok.test: ['.*']\n"
    )
    rc = load_runtime_configuration(f)
    assert rc.access_rules_for(str(tmp_path / "never-launched")).read_policy.is_allowed("ok.test", "/")


def test_load_allow_all_profile(tmp_path):
    scratch = tmp_path / "scratch"
    f = tmp_path / "allowlist.yaml"
    f.write_text(
        "read:\n"
        "  tranco: {enabled: false}\n"
        "profiles:\n"
        f"  {scratch}:\n"
        "    allow_all: true\n"
        "    read:\n"
        "      tranco: {enabled: false}\n"
    )
    policy = load_runtime_configuration(f).access_rules_for(str(scratch))
    # Every action, every host — with the popularity net explicitly opted out of.
    assert policy.read_policy.is_allowed("anything.test", "/x")
    assert policy.rules_for("click", "anything.test", "/x")
    assert policy.rules_for("write-text", "anything.test", "/x")
    # ...and none of it anywhere else.
    other = load_runtime_configuration(f).access_rules_for(str(tmp_path / "other"))
    assert not other.read_policy.is_allowed("anything.test", "/x")
    assert other.rules_for("click", "anything.test", "/x") == []


# -- path resolution ----------------------------------------------------------

def test_resolve_explicit_beats_everything(monkeypatch, tmp_path):
    monkeypatch.setenv("SAFE_AGENT_BROWSER_ALLOWLIST", str(tmp_path / "env.yaml"))
    assert resolve_allowlist_path(str(tmp_path / "cli.yaml")) == tmp_path / "cli.yaml"


def test_resolve_env_beats_user_config(monkeypatch, tmp_path):
    monkeypatch.setenv("SAFE_AGENT_BROWSER_ALLOWLIST", str(tmp_path / "env.yaml"))
    assert resolve_allowlist_path() == tmp_path / "env.yaml"


def test_resolve_user_config_beats_sample(monkeypatch, tmp_path):
    monkeypatch.delenv("SAFE_AGENT_BROWSER_ALLOWLIST", raising=False)
    monkeypatch.setattr(loader_pkg.loader, "USER_CONFIG_DIR", tmp_path)
    user = tmp_path / "allowlist.yaml"
    user.write_text("read:\n  enabled: false\n")
    assert resolve_allowlist_path() == user


def test_resolve_falls_back_to_sample(monkeypatch, tmp_path):
    monkeypatch.delenv("SAFE_AGENT_BROWSER_ALLOWLIST", raising=False)
    monkeypatch.setattr(loader_pkg.loader, "USER_CONFIG_DIR", tmp_path / "absent")
    assert resolve_allowlist_path() == SAMPLE_ALLOWLIST
