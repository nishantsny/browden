# Security Policy

browden's entire purpose is to be a **safety perimeter** around a browser-driving
agent, so its security posture is part of the product, not an afterthought. This
document explains how to report a vulnerability and — just as important — what
browden does and does **not** defend against.

## Reporting a vulnerability

**Please report security issues privately — do not open a public issue for them.**

- Preferred: [**GitHub private vulnerability reporting**](https://github.com/nishantsny/browden/security/advisories/new)
  (the "Report a vulnerability" button on the Security tab).
- Alternatively, email **nishantsny@gmail.com** with `[browden security]` in the
  subject.

Please include enough to reproduce: the browden version / commit, your OS, the
allowlist config in play, and the sequence of tool calls (or a page) that
triggers the issue. A proof-of-concept page or minimal repro helps enormously.

### What to expect

This is a small, volunteer-maintained project, so timelines are best-effort, not
contractual:

- **Acknowledgement** within ~5 days.
- An initial assessment (severity, whether it's in scope) shortly after.
- For confirmed issues, a fix and a coordinated disclosure via a GitHub Security
  Advisory, crediting you unless you prefer to remain anonymous.

Please give a reasonable window to ship a fix before disclosing publicly.

## Supported versions

browden is pre-1.0 and ships from `main`. Security fixes land on `main` and in
the latest tagged release; there is no backporting to older tags. Always run the
latest release or `main`.

| Version | Supported |
| --- | --- |
| latest `main` / latest release | ✅ |
| older tags | ❌ |

## Scope — what is (and isn't) a vulnerability

browden's security model is: **the agent can only reach the tool surface, every
navigation is checked against the read allowlist, and write actions (`click`,
`insert_text`) are default-deny and gated per host + per visible label.** Bugs
that let an agent *escape* that model are in scope. Examples of **in-scope**
issues:

- A URL that passes validation but should have been refused by the configured
  allowlist/denylist (e.g. an allowlist-bypass via host/path parsing, Unicode,
  or redirect handling).
- A write action (`click` / `insert_text`) executed on a host or element that
  the allowlist should **not** have permitted, or a label-regex bypass.
- A page-injected, agent-targeted decoy control that gets actioned despite the
  label policy (the policy is meant to refuse these).
- Any path that lets the agent execute an **unlisted** action against Chrome, or
  reach the DevTools debug port / backend directly.
- Exfiltration of local data (cookies, files, other profiles) beyond what the
  tool surface is designed to expose.

**Out of scope** — these are known, by-design limitations, not vulnerabilities:

- **Prompt injection from an allowlisted page.** browden *shrinks* the read
  surface to established sites; it does **not** make page content trustworthy.
  Reputable, allowlisted sites host untrusted content, and an allowlisted page
  can still try to manipulate the agent. Allowlisting is attack-surface
  reduction, not immunity.
- **Popularity ≠ safety.** The Tranco-based read allowlist uses popularity as a
  proxy for *established*, never a guarantee of *safe*.
- **Whatever your config allows.** If you set `read.enabled: false`, open the web
  with `"*": [".*"]`, or add a host/label to the `click` / `write-text`
  allowlists, the resulting access is intended behavior. Loosen the config and
  you own the consequences.
- **You point it at your real Chrome profile.** Reusing your logged-in profile
  means the agent can read your logged-in pages — that's the documented
  trade-off, not a leak.
- **Anti-bot / ToS enforcement.** browden drives a real, undetected Chrome. Using
  it against sites whose Terms of Service forbid automated access is on the
  operator (see the README disclaimer).
- Vulnerabilities in third-party dependencies (Chrome, Selenium, `mcp`) — report
  those upstream; we'll bump once a fix is released.

If you're unsure whether something is in scope, report it privately and ask.

## Safe harbor

We consider good-faith security research that respects this policy — testing only
against your own installation and profiles, not accessing others' data, and
giving us a chance to remediate before public disclosure — to be authorized, and
we won't pursue action over it.
