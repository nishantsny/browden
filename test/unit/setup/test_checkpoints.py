"""Unit tests for setup/checkpoints.py — the provenance comment-block writer.

No network, no browser: exercises the upsert/merge/render logic against files
in a tmp dir. The block is written as YAML comments, so every line it emits must
start with '#' (invisible to the allowlist loader, greppable by a human).
"""
import hashlib
import sys
from pathlib import Path

SETUP_DIR = Path(__file__).resolve().parents[3] / "setup"
sys.path.insert(0, str(SETUP_DIR))
import checkpoints as cp  # noqa: E402

SAMPLE = (Path(__file__).resolve().parents[3]
          / "configs" / "samples" / "read_only_on_popular_websites.yaml")


def _block_lines(text: str) -> list[str]:
    """The provenance block's lines (sentinels inclusive)."""
    lines = text.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith(cp._BEGIN))
    end = next(i for i in range(start, len(lines)) if lines[i].strip() == cp._END)
    return lines[start:end + 1]


def _values(text: str) -> dict[str, str]:
    _, _, vals = cp._parse_existing(text.splitlines())
    return vals


# -- sha256_hex ---------------------------------------------------------------

def test_sha256_hex_matches_hashlib():
    assert cp.sha256_hex(b"hello") == hashlib.sha256(b"hello").hexdigest()


# -- update_checkpoints: append when no block present -------------------------

def test_appends_block_when_absent(tmp_path):
    f = tmp_path / "allowlist.yaml"
    f.write_text("read:\n  enabled: true\n", encoding="utf-8")
    cp.update_checkpoints(f, {"tranco_id": "XN2NN"})
    text = f.read_text(encoding="utf-8")
    assert text.count(cp._BEGIN) == 1 and text.count(cp._END) == 1
    assert "read:\n  enabled: true\n" in text          # original config untouched
    assert _values(text)["tranco_id"] == "XN2NN"


def test_every_emitted_line_is_a_comment(tmp_path):
    f = tmp_path / "allowlist.yaml"
    f.write_text("read: {}\n", encoding="utf-8")
    cp.update_checkpoints(f, {"tranco_id": "XN2NN", "pal_checksum_sha256": "ab"})
    for ln in _block_lines(f.read_text(encoding="utf-8")):
        assert ln.startswith("#"), f"non-comment line would break YAML: {ln!r}"


# -- update_checkpoints: merge/preserve across partial updates ----------------

def test_partial_updates_preserve_other_keys(tmp_path):
    f = tmp_path / "allowlist.yaml"
    f.write_text("read: {}\n", encoding="utf-8")
    cp.update_checkpoints(f, {"tranco_id": "XN2NN", "tranco_checksum_sha256": "aaa"})
    cp.update_checkpoints(f, {"pal_checksum_sha256": "bbb"})   # a separate PSL refresh
    vals = _values(f.read_text(encoding="utf-8"))
    assert vals == {"tranco_id": "XN2NN",
                    "tranco_checksum_sha256": "aaa",
                    "pal_checksum_sha256": "bbb"}


def test_rewrites_in_place_not_duplicated(tmp_path):
    f = tmp_path / "allowlist.yaml"
    f.write_text("read: {}\n", encoding="utf-8")
    cp.update_checkpoints(f, {"tranco_id": "OLD"})
    cp.update_checkpoints(f, {"tranco_id": "NEW"})
    text = f.read_text(encoding="utf-8")
    assert text.count(cp._BEGIN) == 1                  # not appended twice
    assert _values(text)["tranco_id"] == "NEW"


def test_all_known_keys_render_even_when_unset(tmp_path):
    f = tmp_path / "allowlist.yaml"
    f.write_text("read: {}\n", encoding="utf-8")
    cp.update_checkpoints(f, {"pal_checksum_sha256": "bbb"})
    vals = _values(f.read_text(encoding="utf-8"))
    # tranco_* still present as empty placeholders — stable block shape.
    assert vals == {"tranco_id": "", "tranco_checksum_sha256": "",
                    "pal_checksum_sha256": "bbb"}


def test_idempotent_same_update(tmp_path):
    f = tmp_path / "allowlist.yaml"
    f.write_text("read: {}\n", encoding="utf-8")
    cp.update_checkpoints(f, {"tranco_id": "X", "tranco_checksum_sha256": "a",
                              "pal_checksum_sha256": "b"})
    once = f.read_text(encoding="utf-8")
    cp.update_checkpoints(f, {"tranco_id": "X", "tranco_checksum_sha256": "a",
                              "pal_checksum_sha256": "b"})
    assert f.read_text(encoding="utf-8") == once


# -- update_checkpoints: best-effort, never fatal -----------------------------

def test_missing_file_is_a_quiet_noop(tmp_path):
    cp.update_checkpoints(tmp_path / "nope.yaml", {"tranco_id": "X"})  # must not raise
    assert not (tmp_path / "nope.yaml").exists()


# -- the committed sample already carries an (empty) block --------------------

def test_sample_carries_empty_block_that_populates_in_place(tmp_path):
    f = tmp_path / "allowlist.yaml"
    f.write_text(SAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    assert _values(f.read_text(encoding="utf-8")) == {
        "tranco_id": "", "tranco_checksum_sha256": "", "pal_checksum_sha256": ""}
    cp.update_checkpoints(f, {"tranco_id": "XN2NN"})
    text = f.read_text(encoding="utf-8")
    assert text.count(cp._BEGIN) == 1                  # filled in, not appended
    assert _values(text)["tranco_id"] == "XN2NN"
