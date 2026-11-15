# TaskProof

**Did the agent actually finish the job?** TaskProof checks the files an AI agent left behind, using deterministic rules only — no model calls, no network, no dependencies. It answers `pass`, `partial`, or `fail`, and writes an evidence bundle you can replay.

한국어 설명은 [README.ko.md](README.ko.md)에 있습니다.

## Why

Agent frameworks tell you what the agent *did* — the trace, the tokens, the steps. They rarely tell you whether the work is *correct*. Most "task completion" metrics ask another model to judge, which costs money, varies run to run, and cannot be shown to an auditor.

TaskProof takes the opposite approach: you declare what "done" means, and it checks that against the actual files.

- **Deterministic.** Same inputs, same verdict, same evidence digest. No LLM in the judging path.
- **Local.** Your data never leaves the machine. No API keys, no account, no network.
- **Zero dependencies.** Standard library only — the MCP server included, written against
  the protocol rather than an SDK. Any Python 3.9+.
- **Agent-callable.** An MCP server over stdio, so the agent can verify its own work.
- **Replayable.** Every run writes a manifest with input/output hashes, per-check results, and the exact command to reproduce it.

## Quick start

```bash
python runner.py --spec fixtures/case_incomplete/spec.json
```

```
[PARTIAL] order_payment_reconcile  (model calls: 0)
  - pass    output exists
  - pass    required columns
  - fail    unpaid rows match: expected 3 / actual 2, missing ['A-1005']
  - fail    amount total: expected 142400.00 / actual 132500.00
```

Exit code is `0` for `pass` and `1` otherwise, so it drops straight into CI or a pre-commit hook.

Run the built-in tests — all three verdicts, then the MCP protocol:

```bash
python selftest.py
```

```bash
python test_mcp_server.py
```

## Declaring what "done" means

```json
{
  "task_id": "order_payment_reconcile",
  "inputs":  { "orders": "in/orders.csv", "payments": "in/payments.csv" },
  "outputs": { "missing": "out/missing_orders.csv" },
  "core_output": "missing",
  "checks": [
    { "name": "output exists", "type": "file_exists", "target": "outputs.missing" },
    { "name": "required columns", "type": "csv_columns", "target": "outputs.missing",
      "columns": ["order_id", "amount"] },
    { "name": "unpaid rows match", "type": "reconcile_rows", "orders": "inputs.orders",
      "payments": "inputs.payments", "output": "outputs.missing", "key": "order_id" },
    { "name": "amount total", "type": "amount_sum", "orders": "inputs.orders",
      "payments": "inputs.payments", "output": "outputs.missing",
      "key": "order_id", "amount_column": "amount" }
  ]
}
```

### Checks available today

| Type | What it verifies |
| --- | --- |
| `file_exists` | The named output is actually a file |
| `csv_columns` | The output has the required columns |
| `reconcile_rows` | Rows present in one input and absent in another appear exactly once in the output — recomputed from the inputs, not trusted from the output |
| `amount_sum` | The output total matches the recomputed total, using integer minor units (no floating point) |

### Verdicts

| Verdict | Meaning |
| --- | --- |
| `pass` | Every check passed |
| `partial` | The core output exists and some checks passed |
| `fail` | The core output is missing, or nothing passed |

`partial` exists because "the file was produced but the contents are wrong" is the most common real failure. Treating that as success would make the tool pointless.

## MCP: let the agent verify its own work

TaskProof speaks the [Model Context Protocol](https://modelcontextprotocol.io) over stdio, so an
agent can check its own completion claim instead of waiting for a human to run the CLI afterwards.

```bash
python mcp_server.py --root /abs/path/you/allow
```

| Tool | Purpose |
| --- | --- |
| `verify_task` | Judge a task from its spec file. Returns the verdict, the evidence digest, and per-check detail |
| `replay_evidence` | Re-run a recorded manifest and report whether the digest still matches — for testing a claimed verdict instead of trusting it |
| `describe_checks` | Return the check types and a spec template, so an agent can write a spec before running one |

Register it with any MCP client. For Claude Code or Claude Desktop:

```json
{
  "mcpServers": {
    "taskproof": {
      "command": "python",
      "args": ["/abs/path/to/mcp_server.py", "--root", "/abs/path/you/allow"]
    }
  }
}
```

**No SDK.** The server is written against the protocol directly, standard library only, so there
is still nothing to install. One process serves both eras: the modern `server/discover` handshake
(2026-07-28) and the legacy `initialize` handshake (2025-11-25 and earlier).

**Pass `--root`.** A task definition names the files to read. With `--root`, any path resolving
outside that directory is refused, including paths reached through the spec's own references.
Without it, the server can read whatever this process can.

One deliberate deviation from the modern spec: per-request `_meta` is required only after a client
has probed `server/discover`. A client arriving through `initialize` is not forced to send it,
because that handshake does not establish the modern dialect.

## Evidence bundle

Each run writes `evidence/<task_id>/<digest>/manifest.json`:

- SHA-256 and size of every input and output
- Per-check status, plus the specific missing / unexpected keys
- The verdict, `judged_by: deterministic_rules_only`, and `model_calls: 0`
- An `evidence_digest` computed over the stable fields (timestamps and run ids excluded), so the same inputs always produce the same digest
- A `replay_command` you can paste to reproduce the run

## Scope and support

This is an early release, kept deliberately small.

**Verified:** CSV reconciliation tasks on Windows with Python 3.13; three fixture cases (correct / missing output / incomplete output) judged 3/3 as expected; digest stable across repeated runs. The MCP server is driven as a real subprocess by `test_mcp_server.py` — both handshakes, all three tools, the error contract (protocol faults as JSON-RPC errors, tool failures as `isError` results), evidence replay, and `--root` refusal: 32 checks, all passing.

**Not yet verified:** large files, `.xlsx`, Linux and macOS, concurrent runs, non-UTF-8 encodings beyond BOM handling.

Issues and pull requests are welcome, but please expect a slow, best-effort response from a one-person project. Requests to add checks for a specific recurring task are the most useful kind.

## Roadmap, honestly stated

The MCP interface is in. Planned next: more check types, spreadsheet inputs, and a Linux and macOS run of the test suite.

A paid tier is planned for **team-level features** — audit history across people and runs, approval records, comparison with previous evidence, and CI reporting. Everything documented in this README stays free and Apache-2.0 licensed. The paid tier will be a separate, closed-source add-on; it will not take away what is already here.

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).

Copyright 2026 Sion Jang. The license covers the code; it does not grant rights to the TaskProof name or marks.
