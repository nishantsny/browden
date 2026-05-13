# agentic_evals

This folder holds **agentic evaluation scenarios** — one Markdown file per scenario, written to be read and executed by an AI agent (not by a test runner).

Each Markdown file describes a real consumer scenario for browser-guard. An agent reads the file, **executes** the steps against the running MCP server (and any other MCP servers the scenario names), and then **verifies** the outcome against the pass/fail criteria stated in the file. The point is to exercise the MCP's tools end-to-end the way a real caller would, rather than to assert against mocked internals the way `test/unit/` does.

Conventions:

- One scenario per file; the file name is the scenario name (e.g. `past_5_amazon_orders.md`).
- Each file should state, in plain language: what the agent should do, and the explicit conditions under which the run **passes** or **fails**.
- These runs touch a live browser session and live external sites, so results can legitimately vary (a site changed, you're signed out, the network hiccupped) — a failure here is a signal to investigate, not necessarily a code regression.

## Scenarios

- [`past_5_amazon_orders.md`](past_5_amazon_orders.md) — fetch the 5 most recent Amazon orders and verify their core attributes.
