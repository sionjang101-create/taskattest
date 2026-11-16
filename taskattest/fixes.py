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

"""Structured fix instructions: what to change so a failed check would pass.

An agent that is told "fail" has to read prose to decide what to do next. Every
failed check gets a `fix` object instead: an `action`, the file it concerns, and
the facts the action needs (keys to remove, rows to add, the total it should be).

Fixes state facts; they do not guess at a repair. Two rules keep them from
becoming a way to game the verdict:
- a broken input seal is fixed by restoring the input, never by editing it
  to match the output;
- a failed trace check is a process failure; editing the outputs cannot fix
  it, so the fix says to run the task again.

Fixes live in the manifest's check details, outside the evidence digest, so
their wording can improve without changing what a verdict is evidence of.
"""

from __future__ import annotations

PROCESS_NOTE = ("this checks what happened during the run, not the files left behind; editing "
                "outputs cannot fix it, so run the step again")
SEAL_NOTE = ("an input changed after it was sealed; restore the original input, and do not edit "
             "inputs to make other checks pass")

SCOPE_NOTE = ("these changes are outside what the task was allowed to touch; undo them. If one is "
              "really needed, stop and ask the person to widen the scope; do not edit the spec")


def _file(ctx, args, field="target"):
    ref = args.get(field)
    if isinstance(ref, str):
        return ctx["rel"](ctx["resolve"](ref))
    if isinstance(ref, list):
        return [ctx["rel"](ctx["resolve"](r)) for r in ref if isinstance(r, str)]
    return None


def _input_rows(ctx, args, field, key, keys, load):
    """The input records for the given keys, as facts the agent can copy from."""
    try:
        _, rows, _ = load(ctx, args, field)
    except Exception:  # the check already failed for its own reason; a fix is best-effort
        return []
    wanted = set(keys)
    out = []
    for row in rows:
        value = row.get(key)
        if value is not None and str(value).strip() in wanted:
            out.append(row)
    return out[:50]


def fix_for(item: dict, result: dict, ctx: dict, error: str | None, helpers: dict):
    """Return a fix dict for a failed check, or None when it passed.

    Where the records live inside the file (a JSON array name, an Excel sheet)
    is added, so an agent knows which part of the file to change.
    """
    fix = _fix_for(item, result, ctx, error, helpers)
    if fix and fix.get("file") is not None:
        for key in ("records_key", "sheet", "header_row"):
            if item.get(key) is not None:
                fix[key] = item[key]
    return fix


def _fix_for(item: dict, result: dict, ctx: dict, error: str | None, helpers: dict):
    if result.get("status") != "fail":
        return None
    kind = item.get("type")
    if error == "spec":
        return {"action": "fix_spec", "check": item.get("name", kind), "detail": result.get("detail", ""),
                "note": "the spec itself is incomplete or wrong; this is not a fault in the outputs"}
    if error == "missing":
        detail = result.get("detail", "")
        head = detail.split(" file not found")[0].split()
        field = head[-1] if head else "target"
        if field in ("orders", "payments", "source"):
            return {"action": "provide_input", "file": _file(ctx, item, field), "detail": detail,
                    "note": "an input the check depends on is missing; restore it rather than recreating it"}
        if field == "trace":
            return {"action": "rerun_task", "check": kind, "detail": detail,
                    "note": "the run left no trace to judge; " + PROCESS_NOTE}
        if field not in item:
            field = "output" if kind in ("reconcile_rows", "amount_sum") else "target"
        return {"action": "create_file", "file": _file(ctx, item, field), "detail": detail}

    load = helpers["records"]
    if kind == "file_exists":
        return {"action": "create_file", "file": _file(ctx, item)}
    if kind == "file_hash":
        return {"action": "restore_input", "file": _file(ctx, item),
                "expected_sha256": str(item.get("sha256", "")).lower(), "note": SEAL_NOTE}
    if kind == "csv_columns":
        return {"action": "add_columns", "file": _file(ctx, item),
                "columns": result.get("missing_columns", [])}
    if kind == "row_count":
        bounds = {k: item[k] for k in ("equals", "min", "max") if k in item} or {"min": 1}
        return {"action": "check_record_count", "file": _file(ctx, item),
                "records": result.get("records"), "expected": bounds}
    if kind == "unique_key":
        return {"action": "dedupe", "file": _file(ctx, item), "key": item.get("key", "id"),
                "duplicate_keys": result.get("duplicate_keys", []),
                "rows_without_key": result.get("rows_without_key", 0)}
    if kind == "subset_of":
        return {"action": "remove_records", "file": _file(ctx, item), "key": item.get("key", "id"),
                "values": result.get("invented_keys", []),
                "note": "these keys do not exist in the source; remove them rather than inventing a source"}
    if kind == "reconcile_rows":
        key = item.get("key", "order_id")
        missing = result.get("missing_keys", [])
        fix = {"action": "reconcile", "file": _file(ctx, item, "output"), "key": key,
               "remove": result.get("unexpected_keys", []), "add": missing}
        if missing:
            fix["add_from_input"] = _input_rows(ctx, item, "orders", key, missing, load)
        return fix
    if kind == "amount_sum":
        return {"action": "correct_total", "file": _file(ctx, item, "output"),
                "amount_column": item.get("amount_column", "amount"),
                "expected_total": result.get("expected_total"), "actual_total": result.get("actual_total"),
                "note": "the expected total is recomputed from the inputs; fix the rows, not the inputs"}
    if kind == "numeric_range":
        return {"action": "fix_values", "file": _file(ctx, item), "column": item.get("column"),
                "aggregate": item.get("aggregate", "each"),
                "bounds": {k: item[k] for k in ("min", "max") if k in item},
                "out_of_range": result.get("out_of_range"), "offending": result.get("offending", [])}
    if kind == "json_valid":
        return {"action": "rewrite_as_valid_json", "file": _file(ctx, item),
                "expected_top_level": item.get("top_level"), "detail": result.get("detail", "")}
    if kind == "json_keys":
        return {"action": "add_keys", "file": _file(ctx, item),
                "missing_paths": result.get("missing_paths", []), "empty_paths": result.get("empty_paths", [])}
    if kind == "text_contains":
        return {"action": "edit_text", "file": _file(ctx, item),
                "add": result.get("missing_strings", []), "remove": result.get("forbidden_present", [])}
    if kind == "no_placeholders":
        return {"action": "rewrite_lines", "file": _file(ctx, item),
                "lines": result.get("placeholders_found", [])}
    if kind == "scope_changes":
        revert = result.get("denied", []) + result.get("outside_scope", [])
        return {"action": "revert_out_of_scope", "root": result.get("root", "."), "base": result.get("base", "HEAD"),
                "files": [{"path": f["path"], "status": f["status"],
                           "how": ("delete it" if f["status"] in ("untracked", "added") else
                                   f"restore it from {result.get('base', 'HEAD')}")} for f in revert],
                "changed": len(result.get("changed", [])), "max_changed": result.get("max_changed"),
                "note": SCOPE_NOTE}
    if kind and kind.startswith("trace_"):
        fix = {"action": "rerun_task", "check": kind, "note": PROCESS_NOTE}
        for k in ("first_violation", "incomplete_groups", "count", "final_step", "violations"):
            if k in result:
                fix[k] = result[k]
        return fix
    return {"action": "review", "detail": result.get("detail", "")}


def summary(fix: dict) -> str:
    """One line a person can read; the structured fix is what an agent should use."""
    if not fix:
        return ""
    a = fix["action"]
    f = fix.get("file")
    if a == "remove_records":
        return f"remove {fix['key']} {fix['values']} from {f}"
    if a == "reconcile":
        parts = []
        if fix["add"]:
            parts.append(f"add {fix['key']} {fix['add']}")
        if fix["remove"]:
            parts.append(f"remove {fix['key']} {fix['remove']}")
        return f"{' and '.join(parts)} in {f}"
    if a == "correct_total":
        return f"total of {fix['amount_column']} in {f} should be {fix['expected_total']}, is {fix['actual_total']}"
    if a == "restore_input":
        return f"restore {f} to its sealed original"
    if a == "revert_out_of_scope":
        paths = [f["path"] for f in fix["files"]]
        if not paths:
            return f"{fix['changed']} files changed, more than {fix['max_changed']}; undo the changes the task did not need"
        return f"undo changes outside the task scope: {', '.join(paths[:5])}" + (" ..." if len(paths) > 5 else "")
    if a == "rerun_task":
        return "run the step again; editing outputs cannot fix a process failure"
    if a == "create_file":
        return f"create {f}"
    if a == "fix_spec":
        return "the spec is incomplete; fix the spec"
    return f"{a} in {f}" if f else a
