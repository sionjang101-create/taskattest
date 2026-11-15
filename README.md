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

A deliverable task — a JSON summary plus a written report — is declared the same way:

```json
{
  "task_id": "order_deliverable",
  "inputs":  { "source": "in/source.csv" },
  "outputs": { "summary": "out/summary.json", "report": "out/report.md" },
  "core_output": "summary",
  "checks": [
    { "name": "summary fields", "type": "json_keys", "target": "outputs.summary",
      "required": ["generated_for", "items.*.order_id"], "non_empty": ["items"] },
    { "name": "no invented orders", "type": "subset_of", "target": "outputs.summary",
      "records_key": "items", "key": "order_id",
      "source": "inputs.source", "source_key": "order_id" },
    { "name": "report sections", "type": "text_contains", "target": "outputs.report",
      "contains": ["## Summary", "## Items"] },
    { "name": "nothing left unwritten", "type": "no_placeholders", "target": "outputs.report" },
    { "name": "input not tampered with", "type": "file_hash", "target": "inputs.source",
      "sha256": "<the digest of the file as handed to the agent>" }
  ]
}
```

### Checks available today

Thirteen check types over three artifact families.

**Any artifact**

| Type | What it verifies |
| --- | --- |
| `file_exists` | The named output is actually a file |
| `file_hash` | The file still matches a SHA-256 recorded earlier |

**Record sets** — CSV, TSV, a JSON array, or JSONL. Name the array inside a JSON object with `records_key`.

| Type | What it verifies |
| --- | --- |
| `csv_columns` | Every required column is present |
| `row_count` | The record count fits `equals` / `min` / `max`. With no bounds given, simply that the artifact is not empty |
| `unique_key` | No duplicate keys, and no record missing the key |
| `subset_of` | Every key in the output exists in the source |
| `numeric_range` | Values stay within bounds — `each`, or aggregated by `sum` / `min` / `max` / `count` |
| `reconcile_rows` | Keys present in one input and absent from the other appear exactly once in the output — recomputed from the inputs, not trusted from the output |
| `amount_sum` | The output total matches the recomputed total |

**JSON documents**

| Type | What it verifies |
| --- | --- |
| `json_valid` | The file parses, and optionally that the top level is the expected kind — catches prose, a markdown fence, or a truncated write |
| `json_keys` | Required dotted paths exist and listed paths are not empty. A `*` step visits every array element, as in `items.*.order_id` |

**Text deliverables**

| Type | What it verifies |
| --- | --- |
| `text_contains` | Required strings appear and forbidden strings do not |
| `no_placeholders` | No `TODO`, `lorem ipsum`, `{{`, `I cannot provide` or similar left in the deliverable |

Two of these exist because of how *agents* fail rather than how programs fail. `subset_of` catches
a plausible row that was never in the input. `file_hash` catches an input quietly edited so the
rest of the checks would pass.

Numbers are compared in integer minor units, so no float arithmetic is involved anywhere and
precision is two decimals. A check that cannot be carried out fails; it never passes by default.

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

**Verified:** two task families on Windows with Python 3.13 — CSV reconciliation, and a JSON-plus-report deliverable. Five fixture cases judged as expected with digests stable across repeated runs; in the deliberately flawed deliverable, each of the seven seeded flaws is caught by its own check and nothing else fails (19 assertions in `selftest.py`). The MCP server is driven as a real subprocess by `test_mcp_server.py` — both handshakes, all three tools, the error contract (protocol faults as JSON-RPC errors, tool failures as `isError` results), evidence replay, and `--root` refusal (32 assertions). All passing.

**Not yet verified:** large files, `.xlsx`, Linux and macOS, concurrent runs, non-UTF-8 encodings beyond BOM handling.

Issues and pull requests are welcome, but please expect a slow, best-effort response from a one-person project. Requests to add checks for a specific recurring task are the most useful kind.

## Roadmap, honestly stated

The MCP interface and the wider check set are in. Planned next: spreadsheet (`.xlsx`) inputs, a Linux and macOS run of the test suite, and large-file handling.

A paid tier is planned for **team-level features** — audit history across people and runs, approval records, comparison with previous evidence, and CI reporting. Everything documented in this README stays free and Apache-2.0 licensed. The paid tier will be a separate, closed-source add-on; it will not take away what is already here.

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).

Copyright 2026 Sion Jang. The license covers the code; it does not grant rights to the TaskProof name or marks.
