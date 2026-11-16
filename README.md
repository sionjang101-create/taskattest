# taskattest

<!-- mcp-name: io.github.sionjang101-create/taskattest -->

**Stop a coding agent from finishing with changes outside its task.** taskattest is a completion gate: when the agent tries to stop, it checks what the agent changed against the files it was allowed to touch, what it produced (invented records, wrong totals, CSV and Excel), and, only if you approved them, the commands you named. A failure blocks the stop with a structured fix; the same failure three times hands over to you. Deterministic rules only — no model calls, no network, no dependencies — and every run leaves an evidence bundle you can replay.

In Claude Code:

```text
/plugin marketplace add sionjang101-create/taskattest
/plugin install taskattest@taskattest
```

Your own uncommitted work is recorded when the session starts, so it is never blamed on the agent. Without a definition of done in `.taskattest/spec.json` the gate does nothing; the `define-done` skill writes one with you.

한국어 설명은 [README.ko.md](README.ko.md)에 있습니다.

## Why

Agent frameworks tell you what the agent *did* — the trace, the tokens, the steps. They rarely tell you whether the work is *correct*. Most "task completion" metrics ask another model to judge, which costs money, varies run to run, and cannot be shown to an auditor.

taskattest takes the opposite approach: you declare what "done" means, and it checks that against the actual files.

- **Deterministic.** Same inputs, same verdict, same evidence digest. No LLM in the judging path.
- **Local.** Your data never leaves the machine. No API keys, no account, no network.
- **Zero dependencies.** Standard library only — the MCP server written against the
  protocol rather than an SDK, and the Excel reader built on `zipfile` and `ElementTree`
  rather than a spreadsheet library. Any Python 3.10+.
- **Agent-callable.** An MCP server over stdio, so the agent can verify its own work.
- **Replayable.** Every run writes a manifest with input/output hashes, per-check results, and the exact command to reproduce it.

## Try it in 60 seconds

```bash
git clone https://github.com/sionjang101-create/taskattest && cd taskattest
python -m taskattest --spec examples/agent_says_done/spec.json
```

An agent was asked to list the unpaid orders and replied *done, 3 unpaid orders*. Three were
expected and three were listed — and the answer is still wrong: one order was invented and one was
missed. See [examples/agent_says_done](examples/agent_says_done/README.md).

## Install

```bash
pip install taskattest
```

`pipx install taskattest` or `uv tool install taskattest` work too. There is nothing else to install: the wheel declares no dependencies, and a clean virtual environment with it in holds exactly one distribution.

From a checkout, with nothing installed at all:

```bash
python -m taskattest --spec fixtures/case_incomplete/spec.json
```

## Quick start

```bash
taskattest --spec fixtures/case_incomplete/spec.json
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
python test_xlsx.py
```

```bash
python test_trace.py
```

```bash
python test_draft.py
```

```bash
python test_examples.py
```

```bash
python test_fixes.py
```

```bash
python test_gate.py
```

```bash
python test_scope.py
```

```bash
python test_plugin.py
```

```bash
python test_commands.py
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

Twenty check types over six families. Command, scope and trace checks are described in their own sections below.

**Any artifact**

| Type | What it verifies |
| --- | --- |
| `file_exists` | The named output is actually a file |
| `file_hash` | The file still matches a SHA-256 recorded earlier |

**Record sets** — CSV, TSV, Excel (`.xlsx` / `.xlsm`), a JSON array, or JSONL. Name the array inside a JSON object with `records_key`; pick a worksheet with `sheet` (a name or a 1-based number) and `header_row`. A field other than `target` takes these options prefixed with its own name, as in `source_sheet` or `orders_sheet` — so one check can compare an `.xlsx` output against a `.csv` input.

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

### Commands: the tests pass, not only the files look right

`command_succeeds` runs a command and checks its exit code, so "the tests pass" can be part of done.

```json
{"type": "command_succeeds", "name": "unit tests pass", "cwd": "..",
 "command": ["{python}", "-m", "unittest", "discover", "-s", "tests", "-t", "."], "timeout": 120}
```

Running a command from a spec is a privilege. The gate runs as the person, outside the agent's
permission system: an agent allowed to edit files but not to run commands could otherwise write a
command into the spec and have the gate run it. So:

- a command runs only after a person approved that exact command and working directory with
  `taskattest approve --spec .taskattest/spec.json`. Approvals live outside the project, in
  `~/.taskattest/approved_commands.json` (or `$TASKATTEST_HOME`), so editing a file in the project
  cannot grant one. Changing the command or its directory needs a new approval;
- an unapproved command is not run; the check fails and the fix asks for a person's approval, with the exact `approve` command for this install in `approve_with`;
- the command is an argument list run without a shell, with a time limit (default 300 s, at most
  3600). `{python}` is replaced by the interpreter running taskattest. A single string is refused.

The command and its exit code are evidence. The output tail is shown in the details and the fix
but kept out of the digest, because test output carries timings that change from run to run. A
failed command's fix carries that tail and tells the agent to change the work, not the command or
the tests' expectations.

### Scope: what the agent changed, not only what it delivered

A correct deliverable can come with edits nobody asked for: a config, a lockfile, a test that was
in the way, a file in another module. In the same study of 20,574 sessions, constraint violations
were 38.33% of the problems found and overreach 10.20%. `scope_changes` reads the git working tree
and fails when a changed path is outside the declared scope.

```json
{"type": "scope_changes", "name": "only billing and its tests", "root": "..",
 "allow": ["src/billing/**", "tests/**"], "deny": ["**/*.lock"]}
```

| Field | Meaning |
| --- | --- |
| `allow` | globs the task may change. `*` stays in one directory, `**` crosses directories |
| `deny` | globs that may never change, even when `allow` covers them |
| `ignore` | globs left out of the judgment |
| `root` | the directory to judge, relative to the spec (`..` for a spec in `.taskattest/`) |
| `base` | the git ref to compare against, default `HEAD` |
| `max_changed` | an upper bound on the number of changed files |

Modified, added, deleted and new untracked files all count; files git ignores and anything under
`.taskattest/` do not. Each changed file is recorded with its content hash. The fix lists every
out-of-scope file with how to undo it (restore from the base, or delete a new file) and tells the
agent to ask the person instead of editing the spec.

The person's own uncommitted work is not the agent's. With the gate's start hook (below), the paths
already dirty when the session started are recorded with their hashes and left out, unless the
agent changes them further. Discarding the person's uncommitted edit is reported as a change too.

### Traces: what the agent did, not only what it left behind

Output checks ask whether the files are right. Many agent and robot failures happen on the way:
a forbidden step taken and undone, steps done out of order, a collision recovered by luck. A
trace check reads a time-ordered state log and judges the process.

| Type | What it verifies |
| --- | --- |
| `trace_always` | An invariant holds at every step, or at every step where `when` holds |
| `trace_eventually` | Something happens at least once |
| `trace_final` | The last step satisfies a condition |
| `trace_count` | How many steps match, how many times the state was entered (`edges`), or how many distinct values of a field occur (`distinct`) |
| `trace_order` | Steps happen in order — across the whole trace, or inside each group such as a job or a retry loop (`group_by`) |

A trace is JSONL, a JSON array, CSV, or a raw log: with `line_prefix`, only lines that start with
it are records, so a runner's log can be judged as it was written. UTF-16 logs are detected by
their byte order mark. `target` may list several files, read in order.

Conditions are data, never code, so nothing in a spec is ever evaluated:

```json
{ "all": [
    { "field": "phase", "eq": "commit" },
    { "within": { "fields": ["pos.x", "pos.y"], "point": [1.0, 2.0], "radius": 0.05 } }
] }
```

Operators are `eq`, `ne`, `lt`, `le`, `gt`, `ge`, `in`, `startswith`, `exists`, combined with
`all`, `any`, `not`; `within` is a distance, compared squared so no square root is taken. Numbers
compare as decimals parsed from their text. A missing field never satisfies a comparison, an
invariant checked over zero steps fails, and an unknown operator is an error, not a silent false.

**Pair a trace check with a final-state check.** A log can record that an action happened when
something later undid its result. The log records the action; only the final state records the
outcome. Require both.

### Reading Excel

`.xlsx` is a ZIP of XML, so [xlsx.py](xlsx.py) reads it with `zipfile` and `ElementTree` and no
spreadsheet library. It handles shared strings including values split across formatting runs,
inline strings, booleans, formula results, error cells such as `#DIV/0!`, cells skipped in the
XML (so columns stay aligned with the header), numbers normalised out of scientific notation
without float arithmetic, and dates and times converted to ISO 8601 for both the 1900 and 1904
epochs.

It does not write, and it reads values as stored rather than as displayed: a cached formula
result instead of the formula, and no merged cells, charts or pivot tables. Excel's non-existent
1900-02-29 serial is left as a raw number rather than invented into a date.

### Fixes: what to change, not only what failed

Every failed check carries a `fix`: an action, the file, and the facts the action needs. An agent
can act on it without parsing prose.

```json
{"action": "reconcile", "file": "out/unpaid.csv", "key": "order_id",
 "remove": ["A-1009"], "add": ["A-1005"],
 "add_from_input": [{"order_id": "A-1005", "customer": "Blue Spring Co", "amount": "9900"}]}
```

Fixes state facts; they do not guess at a repair. Two rules keep them from becoming a way to game a
verdict: a broken input seal says to restore the input, never to edit it, and a failed trace check
says to run the step again, because editing outputs cannot fix a process failure. Fixes live in the
check details, outside the evidence digest.

`test_fixes.py` gives a minimal agent nothing but the fixes: applying them turns both seeded
failure cases into a pass.

### Verdicts

| Verdict | Meaning |
| --- | --- |
| `pass` | Every check passed |
| `partial` | The core output exists and some checks passed |
| `fail` | The core output is missing, or nothing passed |

`partial` exists because "the file was produced but the contents are wrong" is the most common real failure. Treating that as success would make the tool pointless.

## Claude Code plugin

The repository is also a Claude Code plugin and its own marketplace. The plugin carries the package
source, so nothing is pip-installed: it needs only Python 3.10 or newer.

```text
/plugin marketplace add sionjang101-create/taskattest
/plugin install taskattest@taskattest
```

| Part | What it does |
| --- | --- |
| Stop hook | runs the completion gate when the agent tries to finish |
| SessionStart hook | records the uncommitted work already present, for the scope check |
| MCP server | `verify_task`, `replay_evidence`, `draft_spec`, `describe_checks`, confined to the project |
| `define-done` skill | writes `.taskattest/spec.json` with the person: what the deliverable is, what may change |

Without a spec the hooks do nothing, so the plugin is quiet until a task has a definition of done.
Set the plugin's `python` option if `python3` is not a real Python on your machine. On Windows it is
often the Store stub; use `python` or the full path to `python.exe`. If Python cannot run, the hooks
fail without blocking the agent.

Verified with Claude Code 2.1.325 by loading the plugin from a checkout (`--plugin-dir`), not yet by
installing it from the marketplace. In a headless run the agent was asked to define done (only
`src/billing` and `tests` may change) and fix a bug. It loaded the skill, used `describe_checks` and
`verify_task`, wrote the spec, fixed the bug in scope, and the Stop hook passed. With the default
`python3` on Windows the hooks failed and the session went on, as intended. The spec it wrote had a
file check and the scope check but nothing that runs the tests; the command check below was added
after that run.

With the command check, a person approved `python -m unittest` for a project whose tests held a
requirement the prompt did not mention (negative quantities must raise). The agent read the tests,
fixed both, and reported the change as untested because it had no permission to run them. The Stop
hook ran the approved tests and recorded exit 0 as evidence. The blocking path, where the tests
still fail when the agent stops, is covered by `test_commands.py`, not by a live run.

## Completion gate for coding agents

A coding agent's "done" is a claim. The gate checks it before the agent can stop. In a study of
20,574 real coding-agent sessions, agents misreported the status of their own work in 22.58% of
the problems found, and 91.49% of the problems that got resolved needed the developer to push back
([arXiv 2605.29442](https://arxiv.org/html/2605.29442v1)). The gate does that pushback with rules.

Put the definition of done in `.taskattest/spec.json` (draft one with `taskattest draft`, then
review it), and add the gate as a Stop hook. For Claude Code, in `.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [{ "hooks": [{ "type": "command", "command": "taskattest gate start", "timeout": 60 }] }],
    "Stop": [{ "hooks": [{ "type": "command", "command": "taskattest gate", "timeout": 60 }] }]
  }
}
```

| When the agent tries to stop | The gate |
| --- | --- |
| no `.taskattest/spec.json` | lets it stop. Absence of a definition of done is not a failure |
| the spec passes | lets it stop, and leaves an evidence bundle |
| the spec fails | blocks the stop; the reason lists each failure with its structured fix |
| the same failure a third time | stops blocking and tells the person, with the evidence. An agent that cannot fix something is not trapped (`TASKATTEST_GATE_REPEATS` changes the threshold) |

`taskattest gate start` matters only for a `scope_changes` check: it records the uncommitted work already present when the session starts, in `.taskattest/baselines/`, so it is not blamed on the agent. A resumed or compacted session keeps its first baseline. The baseline lives in the project, so it guards against mistakes and overreach, not against an agent determined to rewrite it.

Verified with Claude Code 2.1.325 in headless mode: an agent told to reply "done" without
checking a wrong output was blocked, removed the invented row and added the missing one from the
fix, and stopped on the second gate run with a pass. Codex and Cursor also have stop hooks; the
gate has not been tested with them yet.

The scope check was run the same way, in a throwaway repository whose scope allowed `src/billing/**`
and `tests/**`. The first live run exposed a false positive: files that existed before the session
were blamed on the agent, which correctly refused to delete them and was handed over to a person.
That is what the start-hook baseline fixes. After the fix, an agent asked only to fix the billing bug
stopped with a pass. An agent asked also to bump the version in `pyproject.toml` made both edits, was
blocked, did not edit the spec or revert the edit the person had asked for, and asked the person to
choose; the gate handed over on the third identical failure. The second case was induced by the
request, not observed in the wild.

## MCP: let the agent verify its own work

taskattest speaks the [Model Context Protocol](https://modelcontextprotocol.io) over stdio, so an
agent can check its own completion claim instead of waiting for a human to run the CLI afterwards.

```bash
taskattest-mcp --root /abs/path/you/allow
```

| Tool | Purpose |
| --- | --- |
| `verify_task` | Judge a task from its spec file. Returns the verdict, the evidence digest, and per-check detail |
| `replay_evidence` | Re-run a recorded manifest and report whether the digest still matches — for testing a claimed verdict instead of trusting it |
| `draft_spec` | Read the files and propose a spec, plus a profile of every field; the draft is marked and warns until reviewed |
| `describe_checks` | Return the check types and a spec template, so an agent can write a spec before running one |

Register it with any MCP client. For Claude Code or Claude Desktop:

```json
{
  "mcpServers": {
    "taskattest": {
      "command": "taskattest-mcp",
      "args": ["--root", "/abs/path/you/allow"]
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

## Drafting a spec from the files

Writing a spec by hand means opening the files first — and for a raw UTF-16 log, that is the
slow part. `taskattest draft` reads them and proposes one:

```bash
taskattest draft --input orders=in/orders.csv --output picked=out/picked.csv --out spec.json
```

```bash
taskattest draft --trace logs/session01.log --trace logs/session02.log --out spec.json
```

Over MCP, the `draft_spec` tool does the same and also returns a profile of every field — types,
ranges, the values of low-cardinality fields — so an agent can write its own conditions without
opening a file.

What it proposes, and why each is safe to propose:

| From | Proposed |
| --- | --- |
| Inputs | `file_hash` seals with the real digest |
| Outputs that are records | required columns, not empty, uniqueness for instance ids (`id`, `*_id`, `uuid`, `*_no`), `subset_of` relating every id or code back to an input that has the same column, `min: 0` on amount-like columns |
| JSON documents | parses as an object, required top-level keys, and the record checks for any array of objects inside |
| Text | no placeholders, and the headings present as required sections |
| Traces | the line prefix, the grouping field and the state field, then `trace_order` over the milestones common to 80% of groups, and a count of groups reaching the last one |

**The danger it is built around.** An agent that drafts a spec from its own wrong output would
pass. So a draft never lets an output decide a threshold:

- Uniqueness is proposed for an instance id whether or not the output is unique today, so a
  duplicate fails. Codes such as `sku` or `barcode` name a kind of thing and repeat by design;
  they are only related back to the input, never required to be unique.
- `min: 0` is decided from the column name and the inputs. A negative value in the output cannot
  switch off the check that would catch it. If an input already has negative amounts — refunds,
  say — no bound is proposed and the notes say why.
- No upper bound and no trace invariant is ever fitted. Group counts are set to every group
  observed, the strictest value, with a note to lower them to your threshold.
- Column and key checks mirror the file as it is, and the notes say so: they cannot catch a column
  the agent left out. Draft before the run or from a known-good example; the seals and the checks
  against the inputs are what carry correctness.

A drafted spec carries `"draft": true`. Every verdict made with it includes a warning that it is an
unreviewed draft, until a person removes the flag. Drafting over an existing spec is refused.

## Evidence bundle

Each run writes `evidence/<task_id>/<digest>/manifest.json`:

- SHA-256 and size of every input and output
- Per-check status, plus the specific missing / unexpected keys
- The verdict, `judged_by: deterministic_rules_only`, and `model_calls: 0`
- An `evidence_digest` computed over the stable fields (timestamps and run ids excluded), so the same inputs always produce the same digest
- A `replay_command` you can paste to reproduce the run

## Scope and support

This is an early release, kept deliberately small.

**Verified** on Windows with Python 3.11.15 and 3.12.10, 239 assertions across eleven suites, all passing on both. CI runs the same eleven suites on Linux, macOS and Windows with Python 3.10, 3.12 and 3.13, and checks that the built wheel installs with nothing else alongside it.

- `selftest.py` (19): CSV reconciliation and a JSON-plus-report deliverable. Five fixture cases judged as expected with digests stable across repeated runs; in the deliberately flawed deliverable, each of the seven seeded flaws is caught by its own check and nothing else fails.
- `test_xlsx.py` (25): a workbook whose XML is spelled out by hand, asserting exactly what the reader returns for each awkward cell, then a full task judged from `.xlsx` with an `.xlsx` output reconciled against a `.csv` input.
- `test_trace.py` (29): the condition language, a multi-step job judged from a raw UTF-16 log with noise lines — a clean run passes every trace check and a flawed one fails exactly the checks aimed at its flaws — fail-closed cases, and `--root` confinement of list targets.
- `test_draft.py` (33): what a draft proposes, and what it refuses — a wrong output (duplicated id, invented order, negative amount) still fails its own draft, codes are not forced unique, inputs with refunds suppress the bound and say why, traces, determinism, and overwrite safety.
- `test_commands.py` (22): an unapproved command never runs and an approvals file inside the project grants nothing; approval covers the exact command and directory; exit codes decide, with `expect_exit`; varying output leaves the digest unchanged; time limits, string commands and missing programs; approve refuses without a terminal unless `--yes`; the `--root` guard; the gate blocking until an approved command passes.
- `test_plugin.py` (13): the plugin manifests agree with the package version; both hooks and the MCP entry call the launcher with the configured Python; the launcher allows with no spec, blocks a failing spec, passes a fixed one, starts the MCP server and reaches the CLI.
- `test_scope.py` (31): glob semantics; modified, new and deleted files outside the scope each reported with how to undo them; deny over allow; ignored and git-ignored paths; a subdirectory root; non-ASCII names; spec errors; the `--root` guard; the session baseline (pre-existing work left out, further changes and discarded work caught, resume keeps the baseline); the gate blocking and then passing.
- `test_gate.py` (10): the Stop-hook protocol — no spec allows, failure blocks with fixes, the same failure hands over on the third try, a fixed task passes, every run leaves evidence.
- `test_fixes.py` (9): an agent that follows only the fixes turns both seeded failure cases into a pass, and fixes never tell it to edit inputs or to patch over a process failure.
- `test_examples.py` (8): the 60-second example produces exactly the output its README shows, and its input seal survives checkout on every platform.
- `test_mcp_server.py` (40): the server driven as a real subprocess — both handshakes, all four tools, the error contract (protocol faults as JSON-RPC errors, tool failures as `isError` results), evidence replay, `--root` refusal, and `draft_spec` including its draft warning.

The package itself was verified by installing it, not by claiming it: the wheel was built, installed into a clean virtual environment, and run from outside the repository — `pip list` there shows `pip` and `taskattest` and nothing else, and both console scripts and the MCP handshake work from that install.

**Not yet verified:** large files, Linux and macOS, concurrent runs, non-UTF-8 encodings beyond BOM handling, and Excel workbooks produced by anything other than the test writer — the reader is tested against XML written to the specification, not against a file saved by Excel itself.

Issues and pull requests are welcome, but please expect a slow, best-effort response from a one-person project. Requests to add checks for a specific recurring task are the most useful kind.

## Roadmap, honestly stated

The MCP interface, the wider check set and Excel reading are in. Planned next: a Linux and macOS run of the suites, workbooks saved by Excel itself, large-file handling, and streaming instead of reading a whole sheet into memory.

A paid tier is planned for **team-level features** — audit history across people and runs, approval records, comparison with previous evidence, and CI reporting. Everything documented in this README stays free and Apache-2.0 licensed. The paid tier will be a separate, closed-source add-on; it will not take away what is already here.

## Related work

**[taskproof](https://github.com/HaoHaoP/taskproof)** is an unrelated project, published before
this one, with a near-identical premise: never trust an agent's claim that it finished. It
dispatches work to coding agents and then runs an *acceptance command* you supply, whose exit code
decides. It also confirms the worktree really changed, enforces forbidden paths, and keeps a
ledger — plus a web board, a read-only REST API and a desktop console.

Having read its source, the two fit together rather than compete. That project decides with an exit
code. This one *is* an exit code worth deciding with:

```toml
[[taskgroup]]
id = "reports"
project = "reports"
verify = "taskattest --spec .taskattest/spec.json"
verify_kind = "check"
```

See [examples/with_taskproof](examples/with_taskproof/README.md). This follows taskproof's documented configuration and has not yet been run inside taskproof itself.

What this adds:

- **Typed checks rather than a hand-written command.** An acceptance command catches only what its
  author thought to check. `subset_of` catches a record that was never in the input and
  `file_hash` catches an input edited so the rest of the checks would pass, whether or not anyone
  anticipated them.
- **It opens the artifacts.** CSV, TSV, Excel, JSON, JSONL. That project never reads a file's
  contents; it checks that files changed, not whether the contents are right.
- **Reproducible evidence.** The digest is content-addressed over inputs, outputs, checks and
  verdict, with timestamps and run ids excluded, so the same inputs give the same digest on any
  machine and `replay_evidence` re-derives it. That project's tree digest is built from size and
  mtime — a change detector, not evidence.
- **MCP.** The agent can call this mid-task, instead of being wrapped by a CLI adapter.
- **Pure Python.** Its acceptance commands run through `/bin/sh`.

What that project has and this does not: dispatch, worktree isolation, concurrency, forbidden-path
enforcement, a task ledger, agent adapters, a board and a desktop app. It is a system; this is a
checking engine. If you want the system, use theirs — and point its acceptance command here.

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).

Copyright 2026 Sion Jang. The license covers the code.

**On the name.** This project was called *TaskProof* until 2026-11-16. It was renamed to avoid colliding with the unrelated, earlier [taskproof](https://pypi.org/project/taskproof/) described above. It claims no rights in that name.
