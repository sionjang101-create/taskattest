# Copyright 2026 Sion Jang
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""taskattest over the Model Context Protocol, so an agent can verify its own work.

Speaks MCP on stdio using the standard library only. No SDK, no third-party
packages, no network. The judging path is the same deterministic one the CLI
uses, so a verdict reached through MCP is byte-identical to a verdict reached
from the shell.

Protocol eras, both supported on the same process:
  modern (2026-07-28)  the client probes `server/discover` first and carries
                       `protocolVersion` in `params._meta` on every request;
                       results carry `resultType`.
  legacy (<= 2025-11-25)
                       the classic `initialize` handshake; no `resultType`.

Usage
    python mcp_server.py
    python mcp_server.py --root <dir>    refuse any path outside <dir>

Pass --root when an agent drives this server. Without it, a task definition
may point anywhere the operating system lets this process read.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import draft as drafting
from . import runner

SERVER_NAME = "taskattest"
SERVER_TITLE = "taskattest"
SERVER_VERSION = runner.TOOL_VERSION

MODERN_VERSIONS = ["2026-07-28"]
LEGACY_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]

META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_CAPS = "io.modelcontextprotocol/clientCapabilities"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
UNSUPPORTED_PROTOCOL_VERSION = -32022

INSTRUCTIONS = (
    "Verify that a task is actually finished by checking the files that were produced. "
    "Write the definition of done into a spec file first, or call draft_spec to have one "
    "proposed from the files and then review it, then call verify_task. "
    "Judging is deterministic: no model is consulted, so the same inputs always give "
    "the same verdict and the same evidence digest. Call describe_checks to learn the "
    "spec format before writing one."
)


class ToolError(Exception):
    """A failure inside a tool. Reported as a result with isError, not as a protocol error."""


class RpcError(Exception):
    def __init__(self, code: int, message: str, data=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class Session:
    def __init__(self):
        self.result_type = False   # emit `resultType` on results
        self.strict_meta = False   # require per-request _meta (set by server/discover)


# ---------- tool definitions ----------

# Check documentation lives in runner.py, so the CLI and this server can never drift apart.
SPEC_TEMPLATE = {
    "task_id": "order_payment_reconcile",
    "description": "List the orders that have no matching payment.",
    "inputs": {"orders": "in/orders.csv", "payments": "in/payments.csv"},
    "outputs": {"missing": "out/missing_orders.csv"},
    "core_output": "missing",
    "checks": [
        {"name": "output exists", "type": "file_exists", "target": "outputs.missing"},
        {"name": "required columns", "type": "csv_columns", "target": "outputs.missing",
         "columns": ["order_id", "amount"]},
        {"name": "unpaid rows match", "type": "reconcile_rows", "orders": "inputs.orders",
         "payments": "inputs.payments", "output": "outputs.missing", "key": "order_id"},
        {"name": "amount total", "type": "amount_sum", "orders": "inputs.orders",
         "payments": "inputs.payments", "output": "outputs.missing",
         "key": "order_id", "amount_column": "amount"},
    ],
}

SPEC_TEMPLATE_DELIVERABLE = {
    "task_id": "order_deliverable",
    "description": "Summarise the unpaid orders as JSON and write a short report.",
    "inputs": {"source": "in/source.csv"},
    "outputs": {"summary": "out/summary.json", "report": "out/report.md"},
    "core_output": "summary",
    "checks": [
        {"name": "summary parses", "type": "json_valid", "target": "outputs.summary",
         "top_level": "object"},
        {"name": "summary fields", "type": "json_keys", "target": "outputs.summary",
         "required": ["generated_for", "totals.count", "items.*.order_id"],
         "non_empty": ["generated_for", "items"]},
        {"name": "item count", "type": "row_count", "target": "outputs.summary",
         "records_key": "items", "min": 1},
        {"name": "no duplicate items", "type": "unique_key", "target": "outputs.summary",
         "records_key": "items", "key": "order_id"},
        {"name": "no invented orders", "type": "subset_of", "target": "outputs.summary",
         "records_key": "items", "key": "order_id",
         "source": "inputs.source", "source_key": "order_id"},
        {"name": "amounts in range", "type": "numeric_range", "target": "outputs.summary",
         "records_key": "items", "column": "amount", "min": 0, "max": 1000000},
        {"name": "report sections", "type": "text_contains", "target": "outputs.report",
         "contains": ["## Summary", "## Items"], "absent": ["ERROR"]},
        {"name": "nothing left unwritten", "type": "no_placeholders",
         "target": "outputs.report"},
        {"name": "input not tampered with", "type": "file_hash", "target": "inputs.source",
         "sha256": "the digest of the file as it was handed to the agent"},
    ],
}

CHECK_RESULT_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {"name": {"type": "string"},
                       "status": {"type": "string", "enum": ["pass", "fail"]},
                       "detail": {"type": "string"},
                       "fix": {"type": "object",
                               "description": "on failure: an action and the facts needed to make "
                                              "this check pass"}},
        "required": ["name", "status", "detail"],
    },
}

TOOLS = [
    {
        "name": "verify_task",
        "title": "Verify a finished task",
        "description": (
            "Judge whether a task is finished by checking the files it produced against a "
            "spec file that states the definition of done. Returns pass, partial or fail, "
            "plus a replayable evidence bundle. Deterministic: no model is consulted."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "spec_path": {"type": "string",
                              "description": "Path to the task definition JSON."},
                "evidence_dir": {"type": "string",
                                 "description": "Where to write the evidence bundle. "
                                                "Defaults to <spec directory>/evidence."},
            },
            "required": ["spec_path"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "verdict": {"type": "string", "enum": ["pass", "partial", "fail"]},
                "task_id": {"type": ["string", "null"]},
                "evidence_digest": {"type": "string"},
                "evidence_dir": {"type": "string"},
                "replay_command": {"type": "string"},
                "judged_by": {"type": "string"},
                "model_calls": {"type": "integer"},
                "warnings": {"type": "array", "items": {"type": "string"}},
                "checks": CHECK_RESULT_SCHEMA,
            },
            "required": ["verdict", "evidence_digest", "evidence_dir",
                         "judged_by", "model_calls", "checks"],
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False,
                        "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "replay_evidence",
        "title": "Replay an evidence bundle",
        "description": (
            "Re-run the verification recorded in an evidence manifest and report whether the "
            "digest still matches. Use this to test whether a claimed verdict holds up, "
            "rather than taking the manifest's word for it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "manifest_path": {"type": "string",
                                  "description": "Path to a manifest.json from a previous run."},
            },
            "required": ["manifest_path"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "digest_match": {"type": "boolean"},
                "verdict_match": {"type": "boolean"},
                "recorded_digest": {"type": "string"},
                "recomputed_digest": {"type": "string"},
                "recorded_verdict": {"type": "string"},
                "recomputed_verdict": {"type": "string"},
                "spec_path": {"type": "string"},
                "checks": CHECK_RESULT_SCHEMA,
            },
            "required": ["digest_match", "verdict_match", "recorded_digest",
                         "recomputed_digest", "recorded_verdict", "recomputed_verdict"],
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False,
                        "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "draft_spec",
        "title": "Draft a spec from the files",
        "description": (
            "Read the input, output and trace files and propose a spec: structure checks for "
            "outputs, seals on the inputs, checks relating output ids to input ids, and for traces "
            "the ordering and counts it can infer. Also returns a profile of every field so you "
            "can write your own conditions without opening the files. Deterministic, no model "
            "calls. It never fits a threshold to an output; the spec is marked draft and every "
            "verdict against it carries a warning until a person removes the flag."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "spec_path": {"type": "string",
                              "description": "Where the spec will live; paths are made relative "
                                             "to it. Written only if write is true."},
                "inputs": {"type": "object", "additionalProperties": {"type": "string"},
                           "description": "name -> path of files given to the agent"},
                "outputs": {"type": "object", "additionalProperties": {"type": "string"},
                            "description": "name -> path of files the agent must produce"},
                "traces": {"type": "array", "items": {"type": "string"},
                           "description": "state logs, read in order"},
                "line_prefix": {"type": "string",
                                "description": "only lines starting with this are records; "
                                               "detected if omitted"},
                "task_id": {"type": "string"},
                "description": {"type": "string"},
                "write": {"type": "boolean",
                          "description": "write the draft to spec_path; refuses to overwrite"},
            },
            "required": ["spec_path"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "spec": {"type": "object"},
                "notes": {"type": "array", "items": {"type": "string"}},
                "profiles": {"type": "object"},
                "written_to": {"type": ["string", "null"]},
            },
            "required": ["spec", "notes", "profiles", "written_to"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False,
                        "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "describe_checks",
        "title": "Describe the spec format",
        "description": (
            "List the check types this server can run, what each one verifies, and two spec "
            "templates: a record reconciliation and a JSON-plus-report deliverable. Call this "
            "before writing a task definition."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "outputSchema": {
            "type": "object",
            "properties": {
                "tool_version": {"type": "string"},
                "verdict_rules": {"type": "object"},
                "check_types": {"type": "object"},
                "spec_template": {"type": "object"},
                "spec_template_deliverable": {"type": "object"},
            },
            "required": ["tool_version", "verdict_rules", "check_types", "spec_template",
                         "spec_template_deliverable"],
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False,
                        "idempotentHint": True, "openWorldHint": False},
    },
]


# ---------- path confinement ----------

def confine(path: Path, root: Path | None, label: str) -> Path:
    resolved = Path(path).expanduser().resolve()
    if root is not None:
        try:
            resolved.relative_to(root)
        except ValueError:
            raise ToolError(f"{label} is outside the permitted root: {resolved}")
    return resolved


def guard_spec_refs(spec_path: Path, root: Path | None) -> None:
    """Resolve every path a spec refers to and refuse any that escapes the root."""
    if root is None:
        return
    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ToolError(f"spec is not valid JSON: {exc}")
    spec_dir = spec_path.parent.resolve()
    for group in ("inputs", "outputs"):
        for name, value in (spec.get(group) or {}).items():
            confine(spec_dir / value, root, f"{group}.{name}")
    for item in spec.get("checks") or []:
        # "source" was missing here before 0.6.0, so --root did not confine the
        # source side of subset_of. A field may also hold a list of refs (traces).
        for field in ("target", "source", "orders", "payments", "output"):
            refs = item.get(field)
            for ref in (refs if isinstance(refs, list) else [refs]):
                if isinstance(ref, str):
                    confine(runner.resolve_ref(spec_dir, spec, ref), root,
                            f"check {item.get('name', item.get('type', '?'))}.{field}")


# ---------- tools ----------

def tool_verify_task(args: dict, root: Path | None) -> dict:
    raw = args.get("spec_path")
    if not isinstance(raw, str) or not raw.strip():
        raise ToolError("spec_path is required")
    spec_path = confine(Path(raw), root, "spec_path")
    if not spec_path.is_file():
        raise ToolError(f"spec file not found: {spec_path}")
    guard_spec_refs(spec_path, root)

    raw_dir = args.get("evidence_dir")
    if isinstance(raw_dir, str) and raw_dir.strip():
        evidence_root = confine(Path(raw_dir), root, "evidence_dir")
    else:
        evidence_root = spec_path.parent / "evidence"

    m = runner.run(spec_path, evidence_root)
    return {
        "verdict": m["verdict"],
        "task_id": m.get("task_id"),
        "evidence_digest": m["evidence_digest"],
        "evidence_dir": m["evidence_dir"],
        "replay_command": m["replay_command"],
        "judged_by": m["judged_by"],
        "model_calls": m["model_calls"],
        "warnings": m.get("warnings", []),
        "checks": m["check_details"],
    }


def tool_replay_evidence(args: dict, root: Path | None) -> dict:
    raw = args.get("manifest_path")
    if not isinstance(raw, str) or not raw.strip():
        raise ToolError("manifest_path is required")
    manifest_path = confine(Path(raw), root, "manifest_path")
    if not manifest_path.is_file():
        raise ToolError(f"manifest not found: {manifest_path}")
    try:
        recorded = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ToolError(f"manifest is not valid JSON: {exc}")

    spec_raw = recorded.get("spec_path")
    if not isinstance(spec_raw, str):
        raise ToolError("manifest has no spec_path; it predates the spec_path field")
    spec_path = confine(Path(spec_raw), root, "spec_path recorded in the manifest")
    if not spec_path.is_file():
        raise ToolError(f"spec recorded in the manifest no longer exists: {spec_path}")
    guard_spec_refs(spec_path, root)

    # evidence/<task_id>/<digest12>/manifest.json -> evidence root is three levels up
    evidence_root = manifest_path.parent.parent.parent
    fresh = runner.run(spec_path, evidence_root)

    recorded_digest = recorded.get("evidence_digest", "")
    recorded_verdict = recorded.get("verdict", "")
    return {
        "digest_match": recorded_digest == fresh["evidence_digest"],
        "verdict_match": recorded_verdict == fresh["verdict"],
        "recorded_digest": recorded_digest,
        "recomputed_digest": fresh["evidence_digest"],
        "recorded_verdict": recorded_verdict,
        "recomputed_verdict": fresh["verdict"],
        "spec_path": str(spec_path),
        "checks": fresh["check_details"],
    }


def tool_describe_checks(args: dict, root: Path | None) -> dict:
    return {
        "tool_version": SERVER_VERSION,
        "verdict_rules": {
            "pass": "every check passed",
            "partial": "the core output exists and some checks passed",
            "fail": "the core output is missing, or no check passed",
        },
        "check_types": runner.CHECK_DOCS,
        "spec_template": SPEC_TEMPLATE,
        "spec_template_deliverable": SPEC_TEMPLATE_DELIVERABLE,
    }


def tool_draft_spec(args: dict, root) -> dict:
    raw = args.get("spec_path")
    if not isinstance(raw, str) or not raw.strip():
        raise ToolError("spec_path is required")
    spec_path = confine(Path(raw), root, "spec_path")

    def paths(value, label):
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ToolError(f"{label} must be an object of name -> path")
        return {str(k): confine(Path(str(v)), root, f"{label}.{k}") for k, v in value.items()}

    traces = args.get("traces") or []
    if not isinstance(traces, list):
        raise ToolError("traces must be a list of paths")
    trace_paths = [confine(Path(str(t)), root, f"traces[{i}]") for i, t in enumerate(traces)]
    result = drafting.draft_spec(spec_path.parent, paths(args.get("inputs"), "inputs"),
                                 paths(args.get("outputs"), "outputs"), trace_paths,
                                 args.get("line_prefix"), args.get("task_id") or "draft_task",
                                 args.get("description") or "")
    written = None
    if args.get("write"):
        try:
            written = str(drafting.write_spec(result["spec"], spec_path))
        except FileExistsError as exc:
            raise ToolError(str(exc))
    return {"spec": result["spec"], "notes": result["notes"], "profiles": result["profiles"],
            "written_to": written}


TOOL_IMPLS = {
    "draft_spec": tool_draft_spec,
    "verify_task": tool_verify_task,
    "replay_evidence": tool_replay_evidence,
    "describe_checks": tool_describe_checks,
}


# ---------- transport ----------

def log(message: str) -> None:
    print(f"[{SERVER_NAME}] {message}", file=sys.stderr, flush=True)


def send(message: dict) -> None:
    # One message per line, no embedded newlines, nothing but MCP on stdout.
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def ok(req_id, payload: dict) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": payload}


def err(req_id, code: int, message: str, data=None) -> dict:
    body = {"code": code, "message": message}
    if data is not None:
        body["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": body}


def wrap(session: Session, payload: dict) -> dict:
    if session.result_type:
        return {"resultType": "complete", **payload}
    return payload


def require_meta(session: Session, params: dict) -> None:
    """Modern clients must carry the protocol version and capabilities on every request."""
    if not session.strict_meta:
        return
    meta = (params or {}).get("_meta") or {}
    version = meta.get(META_VERSION)
    if version is None:
        raise RpcError(INVALID_PARAMS, f"Missing required _meta field: {META_VERSION}")
    if version not in MODERN_VERSIONS:
        raise RpcError(UNSUPPORTED_PROTOCOL_VERSION, "Unsupported protocol version",
                       {"supported": MODERN_VERSIONS, "requested": version})
    if META_CLIENT_CAPS not in meta:
        raise RpcError(INVALID_PARAMS, f"Missing required _meta field: {META_CLIENT_CAPS}")


def handle_discover(session: Session, params: dict) -> dict:
    session.result_type = True
    session.strict_meta = True
    return {
        "resultType": "complete",
        "supportedVersions": list(MODERN_VERSIONS),
        "capabilities": {"tools": {}},
        "instructions": INSTRUCTIONS,
        "_meta": {META_SERVER_INFO: {"name": SERVER_NAME, "title": SERVER_TITLE,
                                     "version": SERVER_VERSION}},
    }


def handle_initialize(session: Session, params: dict) -> dict:
    requested = (params or {}).get("protocolVersion")
    known = MODERN_VERSIONS + LEGACY_VERSIONS
    chosen = requested if requested in known else LEGACY_VERSIONS[0]
    # A modern version negotiated here means results carry resultType. Per-request
    # _meta stays optional on this path, because only server/discover establishes
    # that the client speaks the modern dialect.
    session.result_type = chosen in MODERN_VERSIONS
    return {
        "protocolVersion": chosen,
        "capabilities": {"tools": {}},
        "serverInfo": {"name": SERVER_NAME, "title": SERVER_TITLE, "version": SERVER_VERSION},
        "instructions": INSTRUCTIONS,
    }


def handle_tools_list(session: Session, params: dict, root: Path | None) -> dict:
    require_meta(session, params)
    return wrap(session, {"tools": TOOLS})


def handle_tools_call(session: Session, params: dict, root: Path | None) -> dict:
    require_meta(session, params)
    params = params or {}
    name = params.get("name")
    impl = TOOL_IMPLS.get(name)
    if impl is None:
        raise RpcError(INVALID_PARAMS, f"Unknown tool: {name}")
    args = params.get("arguments") or {}
    if not isinstance(args, dict):
        raise RpcError(INVALID_PARAMS, "arguments must be an object")
    try:
        payload = impl(args, root)
    except ToolError as exc:
        return wrap(session, {"content": [{"type": "text", "text": f"error: {exc}"}],
                              "isError": True})
    except Exception as exc:  # a bug here is still the tool's failure, not a protocol fault
        log(f"{name} raised {type(exc).__name__}: {exc}")
        return wrap(session, {"content": [{"type": "text",
                                           "text": f"error: {type(exc).__name__}: {exc}"}],
                              "isError": True})
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    return wrap(session, {"content": [{"type": "text", "text": text}],
                          "structuredContent": payload,
                          "isError": False})


def handle(session: Session, msg: dict, root: Path | None):
    method = msg.get("method")
    if not isinstance(method, str):
        if "id" in msg:
            return err(msg.get("id"), INVALID_REQUEST, "Invalid Request: no method")
        return None
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    is_request = "id" in msg
    req_id = msg.get("id")

    if not is_request:  # notification: never answered
        if method not in ("notifications/initialized", "notifications/cancelled",
                          "notifications/progress", "notifications/roots/list_changed"):
            log(f"ignoring unknown notification: {method}")
        return None

    try:
        if method == "server/discover":
            return ok(req_id, handle_discover(session, params))
        if method == "initialize":
            return ok(req_id, handle_initialize(session, params))
        if method == "ping":
            require_meta(session, params)
            return ok(req_id, wrap(session, {}))
        if method == "tools/list":
            return ok(req_id, handle_tools_list(session, params, root))
        if method == "tools/call":
            return ok(req_id, handle_tools_call(session, params, root))
        return err(req_id, METHOD_NOT_FOUND, f"Method not found: {method}")
    except RpcError as exc:
        return err(req_id, exc.code, exc.message, exc.data)
    except Exception as exc:
        log(f"internal error on {method}: {type(exc).__name__}: {exc}")
        return err(req_id, INTERNAL_ERROR, f"Internal error: {type(exc).__name__}")


def serve(root: Path | None) -> int:
    for stream, kwargs in ((sys.stdin, {"encoding": "utf-8"}),
                           (sys.stdout, {"encoding": "utf-8", "newline": "\n"})):
        try:
            stream.reconfigure(**kwargs)
        except (AttributeError, ValueError):  # already wrapped, or not reconfigurable
            pass

    session = Session()
    log(f"{SERVER_NAME} {SERVER_VERSION} on stdio"
        + (f", root={root}" if root else ", no root confinement"))

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            send(err(None, PARSE_ERROR, "Parse error"))
            continue
        if isinstance(msg, list):
            # JSON-RPC batching was removed from MCP; answer the first id so the
            # client is not left waiting.
            first = next((m.get("id") for m in msg if isinstance(m, dict) and "id" in m), None)
            send(err(first, INVALID_REQUEST, "Batch requests are not supported"))
            continue
        if not isinstance(msg, dict):
            send(err(None, INVALID_REQUEST, "Invalid Request"))
            continue
        response = handle(session, msg, root)
        if response is not None:
            send(response)

    log("stdin closed, exiting")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="taskattest MCP server (stdio)")
    ap.add_argument("--root", default=None,
                    help="refuse any path outside this directory; recommended when an "
                         "agent drives the server")
    a = ap.parse_args(argv)
    root = Path(a.root).expanduser().resolve() if a.root else None
    if root is not None and not root.is_dir():
        print(f"--root is not a directory: {root}", file=sys.stderr)
        return 2
    return serve(root)


if __name__ == "__main__":
    sys.exit(main())
