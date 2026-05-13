# agentic_evals

This folder holds **agentic evaluation scenarios** — Markdown files written to be read and executed by an AI agent (not by a test runner).

Each scenario describes a real consumer use of browser-guard. An agent reads the file, **executes** the steps against the running MCP server (and any other MCP servers the scenario names), and then **verifies** the outcome against the pass/fail criteria stated in the file. The point is to exercise the MCP's tools end-to-end the way a real caller would, rather than to assert against mocked internals the way `test/unit/` does.

## Layout

- `explanation.md` — this file.
- `evals/` — one Markdown file per scenario; the file name is the scenario name (e.g. `evals/past_5_amazon_orders.md`).

## Writing a scenario

Each file under `evals/` should state, in plain language: what the agent should do, and the explicit conditions under which the run **passes** or **fails**.

Note that these runs touch a live browser session and live external sites, so results can legitimately vary (a site changed, you're signed out, the network hiccupped) — a failure here is a signal to investigate, not necessarily a code regression.

## Scenarios

- [`evals/past_5_amazon_orders.md`](evals/past_5_amazon_orders.md) — fetch the 5 most recent Amazon orders and verify their core attributes.
