"""Canonicalize a Chrome profile directory. THE one canonicalizer, shared.

A profile *is* its directory: the session layer keys one Chrome process (and
everything that process holds — cookies, extensions, logged-in state) by the
resolved path the caller asked for. Anything else that has to name the same
profile — a config file scoping rules to it, a log line, a test — has to reduce
its spelling to exactly the same path, or ``~/.cache/browden/x`` and
``/home/me/.cache/browden/x`` silently describe two different profiles.

So the reduction lives here, in one function both sides import, the way
:func:`~browden.mcp.validator.tranco.canonical_host` is the one host
canonicalizer every gate compares hosts with.
"""
from pathlib import Path


def canonical_profile_dir(profile_dir: "str | Path") -> Path:
    """The canonical absolute path for ``profile_dir``.

    Expands ``~`` and resolves the result: symlinks followed, ``.``/``..``
    segments collapsed, and a relative path anchored against the process's
    working directory. Non-existent paths resolve fine (a profile directory is
    created by Chrome on first launch, so a config may legitimately name one
    that does not exist yet).
    """
    return Path(profile_dir).expanduser().resolve()
