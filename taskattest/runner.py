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

"""taskattest - did the agent actually finish the job?

Checks the files an AI agent left behind against a declared definition of "done",
using deterministic rules only, and writes a replayable evidence bundle.

Design rules
- Deterministic judging only. No model calls, no network.
- Zero third-party dependencies. Standard library only.
- Same inputs produce the same evidence digest (timestamps and run ids excluded).
- Three verdicts only: pass / partial / fail.
- Fail closed. A check that cannot be carried out fails; it never passes by default.

Checks cover four families: record sets (CSV, TSV, Excel, JSON arrays, JSONL),
JSON documents, text deliverables, and traces - what an agent or robot did, step
by step, read from a state log. Two of them target failures specific to
agents: `subset_of` catches invented records, and `file_hash` catches an input
edited to make the other checks pass.

Usage
    python runner.py --spec <spec.json>
    python runner.py --spec <spec.json> --evidence-dir <dir>
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import trace as trace_mod
from . import xlsx

from . import __version__ as TOOL_VERSION  # single source of the version
CHUNK = 1 << 16
MAX_LISTED = 50  # cap on keys listed in a manifest, so evidence stays small


class CheckFail(Exception):
    """A check that cannot be carried out. Reported as a failed check, never as a pass."""


# ---------- helpers ----------

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def canonical_digest(obj) -> str:
    text = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_records(path: Path, records_key=None, sheet=None, header_row=1) -> list:
    """Read an artifact as a list of records: CSV, TSV, Excel, a JSON array, or JSONL.

    `records_key` names the array inside a JSON object, as in {"items": [...]}.
    `sheet` and `header_row` apply to Excel workbooks and are ignored otherwise.
    """
    suffix = path.suffix.lower()
    if suffix in xlsx.SUFFIXES:
        return xlsx.read_xlsx(path, sheet, header_row)[1]
    if suffix in (".csv", ".tsv"):
        delimiter = "\t" if suffix == ".tsv" else ","
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            return [dict(row) for row in csv.DictReader(f, delimiter=delimiter)]
    if suffix == ".jsonl":
        rows = []
        with path.open("r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        return [r for r in rows if isinstance(r, dict)]

    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if records_key is not None:
        if not isinstance(data, dict):
            raise ValueError(f"records_key {records_key!r} given, but the top level is "
                             f"{json_type_name(data)}")
        if records_key not in data:
            raise ValueError(f"no key {records_key!r} at the top level")
        data = data[records_key]
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    raise ValueError(f"expected an array of records, found {json_type_name(data)}"
                     + ("" if records_key else "; name it with records_key"))


def columns_of(path: Path, rows: list, options=None) -> list:
    """Column names: the declared header for CSV and Excel, the union of keys for JSON."""
    options = options or {}
    suffix = path.suffix.lower()
    if suffix in xlsx.SUFFIXES:
        return xlsx.read_xlsx(path, options.get("sheet"),
                              options.get("header_row", 1))[0]
    if suffix in (".csv", ".tsv"):
        delimiter = "\t" if suffix == ".tsv" else ","
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            return next(csv.reader(f, delimiter=delimiter), [])
    seen = []
    for row in rows:
        for key in row:
            if key not in seen:
                seen.append(key)
    return seen


def key_of(row, key):
    """A record's key as a trimmed string, so CSV "7" and JSON 7 compare equal."""
    if not isinstance(row, dict):
        return None
    value = row.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def json_type_name(value) -> str:
    if isinstance(value, bool):
        return "boolean"
    if value is None:
        return "null"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (int, float)):
        return "number"
    return type(value).__name__


def get_path(data, dotted: str):
    """Walk a dotted path; a `*` step visits every element of an array.

    Returns (values, found). Found is False when a step is absent, and also when a
    `*` step matches nothing at all - an empty array does not satisfy a required path.
    """
    current = [data]
    for part in str(dotted).split("."):
        nxt = []
        for node in current:
            if part == "*":
                if isinstance(node, list):
                    nxt.extend(node)
                elif isinstance(node, dict):
                    nxt.extend(node.values())
                else:
                    return [], False
            elif isinstance(node, dict):
                if part not in node:
                    return [], False
                nxt.append(node[part])
            else:
                return [], False
        current = nxt
    return current, bool(current)


def to_minor_units(value) -> int:
    """Parse an amount into integer minor units, so money never touches float math."""
    if value is None or value is True or value is False:
        return 0
    if isinstance(value, int):
        return value * 100
    if isinstance(value, float):
        value = f"{value:.2f}"  # format once, then integer arithmetic only
    s = str(value).strip().replace(",", "")
    if s == "":
        return 0
    if "." in s:
        whole, frac = s.split(".", 1)
        frac = (frac + "00")[:2]
    else:
        whole, frac = s, "00"
    negative = whole.startswith("-")
    whole = whole.lstrip("+-") or "0"
    if not whole.isdigit() or not frac.isdigit():
        raise CheckFail(f"not a number: {value!r}")
    total = int(whole) * 100 + int(frac)
    return -total if negative else total


def format_amount(minor: int) -> str:
    sign = "-" if minor < 0 else ""
    minor = abs(minor)
    return f"{sign}{minor // 100}.{minor % 100:02d}"


def _source_options(args, field: str) -> dict:
    """Reader options for one field: `target` takes them bare, others prefixed.

    So a check reads `sheet` for its target and `source_sheet` for its source.
    """
    prefix = "" if field == "target" else field + "_"
    return {"records_key": args.get(prefix + "records_key"),
            "sheet": args.get(prefix + "sheet"),
            "header_row": args.get(prefix + "header_row", 1)}


def _records(ctx, args, field: str):
    path = ctx["resolve"](args[field])
    if not path.is_file():
        raise CheckFail(f"{field} file not found")
    options = _source_options(args, field)
    try:
        return path, load_records(path, **options), options
    except (ValueError, json.JSONDecodeError) as exc:
        raise CheckFail(f"{field} is not a readable record set: {exc}")


def _json(ctx, args, field: str = "target"):
    path = ctx["resolve"](args[field])
    if not path.is_file():
        raise CheckFail(f"{field} file not found")
    try:
        return path, json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise CheckFail(f"invalid JSON at line {exc.lineno} column {exc.colno}: {exc.msg}")


def _text(ctx, args, field: str = "target"):
    path = ctx["resolve"](args[field])
    if not path.is_file():
        raise CheckFail(f"{field} file not found")
    return path, path.read_text(encoding="utf-8-sig", errors="replace")


def _listed(items: list) -> list:
    return list(items[:MAX_LISTED])


def _sample(items: list, count: int = 5) -> str:
    shown = ", ".join(str(i) for i in items[:count])
    return shown + (f", +{len(items) - count} more" if len(items) > count else "")


# ---------- checks: any artifact ----------

def check_file_exists(ctx, args) -> dict:
    path = ctx["resolve"](args["target"])
    ok = path.is_file()
    return {"status": "pass" if ok else "fail",
            "detail": f"{'exists' if ok else 'missing'}: {ctx['rel'](path)}"}


def check_file_hash(ctx, args) -> dict:
    """Catches an input quietly edited so the other checks would pass."""
    path = ctx["resolve"](args["target"])
    if not path.is_file():
        raise CheckFail("target file not found")
    expected = str(args.get("sha256") or "").strip().lower()
    if not expected:
        raise CheckFail("file_hash needs the expected sha256")
    actual = sha256_file(path)
    ok = expected == actual
    return {"status": "pass" if ok else "fail",
            "detail": f"expected {expected[:16]}, actual {actual[:16]}",
            "matches": ok}


# ---------- checks: record sets ----------

def check_csv_columns(ctx, args) -> dict:
    path, rows, options = _records(ctx, args, "target")
    wanted = args.get("columns")
    if not wanted:
        raise CheckFail("csv_columns needs a columns list")
    header = columns_of(path, rows, options)
    missing = [c for c in wanted if c not in header]
    return {"status": "pass" if not missing else "fail",
            "detail": f"{len(rows)} records, missing columns: {missing or 'none'}",
            "missing_columns": missing}


def check_row_count(ctx, args) -> dict:
    _, rows, _ = _records(ctx, args, "target")
    count = len(rows)
    equals, low, high = args.get("equals"), args.get("min"), args.get("max")
    if equals is None and low is None and high is None:
        low = 1  # with no bounds given, the artifact must at least not be empty
    problems = []
    if equals is not None and count != int(equals):
        problems.append(f"expected exactly {int(equals)}")
    if low is not None and count < int(low):
        problems.append(f"expected at least {int(low)}")
    if high is not None and count > int(high):
        problems.append(f"expected at most {int(high)}")
    return {"status": "pass" if not problems else "fail",
            "detail": f"{count} records" + ("; " + "; ".join(problems) if problems else ""),
            "records": count}


def check_unique_key(ctx, args) -> dict:
    _, rows, _ = _records(ctx, args, "target")
    key = args.get("key", "id")
    counts: dict = {}
    blank = 0
    for row in rows:
        k = key_of(row, key)
        if k is None:
            blank += 1
            continue
        counts[k] = counts.get(k, 0) + 1
    duplicates = sorted(k for k, c in counts.items() if c > 1)
    ok = not duplicates and blank == 0
    return {"status": "pass" if ok else "fail",
            "detail": (f"{len(rows)} records, {len(duplicates)} duplicate key(s)"
                       + (f" ({_sample(duplicates)})" if duplicates else "")
                       + f", {blank} without {key}"),
            "duplicate_keys": _listed(duplicates), "rows_without_key": blank}


def check_subset_of(ctx, args) -> dict:
    """Catches invented records: every output key must exist in the source."""
    _, out_rows, _ = _records(ctx, args, "target")
    _, src_rows, _ = _records(ctx, args, "source")
    key = args.get("key", "id")
    source_key = args.get("source_key", key)
    allowed = {k for k in (key_of(r, source_key) for r in src_rows) if k is not None}
    seen = [key_of(r, key) for r in out_rows]
    blank = sum(1 for k in seen if k is None)
    invented = sorted({k for k in seen if k is not None and k not in allowed})
    ok = not invented and blank == 0
    return {"status": "pass" if ok else "fail",
            "detail": (f"{len(out_rows)} records against {len(allowed)} permitted key(s), "
                       f"{len(invented)} not in the source"
                       + (f" ({_sample(invented)})" if invented else "")
                       + f", {blank} without {key}"),
            "invented_keys": _listed(invented), "rows_without_key": blank}


def check_numeric_range(ctx, args) -> dict:
    _, rows, _ = _records(ctx, args, "target")
    aggregate = args.get("aggregate", "each")
    if "min" not in args and "max" not in args:
        raise CheckFail("numeric_range needs min, max, or both")
    low = to_minor_units(args["min"]) if "min" in args else None
    high = to_minor_units(args["max"]) if "max" in args else None

    if aggregate == "count":
        values = [len(rows) * 100]
        label = f"count {len(rows)}"
    else:
        column = args.get("column")
        if not column:
            raise CheckFail("numeric_range needs a column unless aggregate is count")
        raw = [r.get(column) for r in rows if isinstance(r, dict)]
        present = [v for v in raw if v is not None and str(v).strip() != ""]
        numbers = [to_minor_units(v) for v in present]
        blank = len(raw) - len(present)
        if aggregate == "each":
            values = numbers
            label = f"{len(numbers)} value(s), {blank} blank"
        elif aggregate in ("sum", "min", "max"):
            if not numbers:
                raise CheckFail(f"no values in column {column!r} to take the {aggregate} of")
            picked = {"sum": sum, "min": min, "max": max}[aggregate](numbers)
            values = [picked]
            label = f"{aggregate} {format_amount(picked)}"
        else:
            raise CheckFail(f"unknown aggregate: {aggregate}")

    out_of_range = [v for v in values
                    if (low is not None and v < low) or (high is not None and v > high)]
    bounds = (f"[{format_amount(low) if low is not None else '-inf'}, "
              f"{format_amount(high) if high is not None else '+inf'}]")
    if not values:
        return {"status": "fail", "detail": f"no values to check, bounds {bounds}",
                "out_of_range": 0, "values_checked": 0}
    return {"status": "pass" if not out_of_range else "fail",
            "detail": f"{label}, bounds {bounds}, {len(out_of_range)} out of range",
            "out_of_range": len(out_of_range), "values_checked": len(values)}


def recompute_unpaid(ctx, args):
    """Recompute, from the inputs, which keys should appear in the output.

    The output file is never trusted as the source of truth.
    """
    _, orders, _ = _records(ctx, args, "orders")
    _, payments, _ = _records(ctx, args, "payments")
    key = args.get("key", "order_id")
    amount_column = args.get("amount_column", "amount")
    paid = {k for k in (key_of(r, key) for r in payments) if k is not None}
    expected = {k for k in (key_of(r, key) for r in orders)
                if k is not None and k not in paid}
    amounts = {}
    for row in orders:
        k = key_of(row, key)
        if k is not None:
            amounts[k] = to_minor_units(row.get(amount_column, 0))
    return expected, amounts


def check_reconcile_rows(ctx, args) -> dict:
    _, out_rows, _ = _records(ctx, args, "output")
    expected, _ = recompute_unpaid(ctx, args)
    key = args.get("key", "order_id")
    actual = {k for k in (key_of(r, key) for r in out_rows) if k is not None}
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    ok = not missing and not unexpected
    return {"status": "pass" if ok else "fail",
            "detail": (f"expected {len(expected)} / actual {len(actual)}, "
                       f"missing {missing or 'none'}, unexpected {unexpected or 'none'}"),
            "missing_keys": _listed(missing), "unexpected_keys": _listed(unexpected)}


def check_amount_sum(ctx, args) -> dict:
    _, out_rows, _ = _records(ctx, args, "output")
    expected_keys, amounts = recompute_unpaid(ctx, args)
    column = args.get("amount_column", "amount")
    expected_total = sum(amounts[k] for k in expected_keys if k in amounts)
    actual_total = sum(to_minor_units(r.get(column, 0)) for r in out_rows)
    return {"status": "pass" if expected_total == actual_total else "fail",
            "detail": (f"expected total {format_amount(expected_total)} / "
                       f"actual total {format_amount(actual_total)}")}


# ---------- checks: JSON documents ----------

def check_json_valid(ctx, args) -> dict:
    path, data = _json(ctx, args)
    kind = json_type_name(data)
    size = len(data) if isinstance(data, (dict, list, str)) else None
    detail = f"valid JSON, top level {kind}"
    if size is not None:
        detail += f" with {size} entr{'y' if size == 1 else 'ies'}"
    expected = args.get("top_level")
    if expected and expected != kind:
        return {"status": "fail", "detail": detail + f", expected {expected}",
                "top_level": kind}
    return {"status": "pass", "detail": detail, "top_level": kind}


def check_json_keys(ctx, args) -> dict:
    path, data = _json(ctx, args)
    required = args.get("required") or []
    non_empty = args.get("non_empty") or []
    if not required and not non_empty:
        raise CheckFail("json_keys needs required, non_empty, or both")
    missing = [p for p in required if not get_path(data, p)[1]]
    empty = []
    for dotted in non_empty:
        values, found = get_path(data, dotted)
        if not found or any(v is None or v == "" or v == [] or v == {} for v in values):
            empty.append(dotted)
    ok = not missing and not empty
    return {"status": "pass" if ok else "fail",
            "detail": f"missing {missing or 'none'}, empty {empty or 'none'}",
            "missing_paths": _listed(missing), "empty_paths": _listed(empty)}


# ---------- checks: text deliverables ----------

def check_text_contains(ctx, args) -> dict:
    path, text = _text(ctx, args)
    contains = args.get("contains") or []
    absent = args.get("absent") or []
    if not contains and not absent:
        raise CheckFail("text_contains needs contains, absent, or both")
    cased = bool(args.get("case_sensitive", False))
    hay = text if cased else text.lower()

    def norm(s):
        return s if cased else str(s).lower()

    missing = [s for s in contains if norm(s) not in hay]
    present = [s for s in absent if norm(s) in hay]
    ok = not missing and not present
    return {"status": "pass" if ok else "fail",
            "detail": (f"{len(text)} chars, missing {missing or 'none'}, "
                       f"forbidden present {present or 'none'}"),
            "missing_strings": _listed(missing), "forbidden_present": _listed(present)}


DEFAULT_PLACEHOLDERS = ["TODO", "FIXME", "TBD", "lorem ipsum", "<placeholder>",
                        "PLACEHOLDER", "{{", "as an AI", "I cannot provide",
                        "your_name_here"]


def check_no_placeholders(ctx, args) -> dict:
    """Catches a deliverable left half-written, or a refusal pasted into one."""
    path, text = _text(ctx, args)
    patterns = list(args.get("patterns") or DEFAULT_PLACEHOLDERS)
    patterns += list(args.get("extra_patterns") or [])
    lowered = [(p, str(p).lower()) for p in patterns]
    found = []
    for number, line in enumerate(text.splitlines(), start=1):
        low = line.lower()
        for original, needle in lowered:
            if needle in low:
                found.append({"pattern": original, "line": number})
    detail = f"{len(patterns)} pattern(s) searched, {len(found)} hit(s)"
    if found:
        detail += " (" + _sample([f"{f['pattern']}@{f['line']}" for f in found]) + ")"
    return {"status": "pass" if not found else "fail", "detail": detail,
            "placeholders_found": _listed(found)}


CHECKS = {
    "file_exists": check_file_exists,
    "file_hash": check_file_hash,
    "csv_columns": check_csv_columns,
    "row_count": check_row_count,
    "unique_key": check_unique_key,
    "subset_of": check_subset_of,
    "numeric_range": check_numeric_range,
    "reconcile_rows": check_reconcile_rows,
    "amount_sum": check_amount_sum,
    "json_valid": check_json_valid,
    "json_keys": check_json_keys,
    "text_contains": check_text_contains,
    "no_placeholders": check_no_placeholders,
}

RECORD_SOURCE = ("a CSV, TSV, Excel (.xlsx/.xlsm), JSON array or JSONL artifact. Name "
                 "the array inside a JSON object with records_key; pick an Excel sheet "
                 "with sheet and header_row. A field other than target takes these "
                 "options prefixed with its own name, as in source_sheet")

CHECK_DOCS = {
    "file_exists": {
        "family": "any artifact",
        "verifies": "the named artifact exists as a file",
        "fields": {"target": "a ref such as outputs.<name>, or a path relative to the spec"},
    },
    "file_hash": {
        "family": "any artifact",
        "verifies": ("the file still matches a sha256 recorded earlier. Seal your inputs "
                     "with this to catch an input edited so the other checks would pass"),
        "fields": {"target": "ref or path", "sha256": "the expected digest"},
    },
    "csv_columns": {
        "family": "record set",
        "verifies": "every required column is present",
        "fields": {"target": f"ref or path to {RECORD_SOURCE}",
                   "columns": "column names that must be present",
                   "records_key": "optional array name inside a JSON object",
                   "sheet": "optional Excel sheet name or 1-based number",
                   "header_row": "optional 1-based Excel header row, default 1"},
    },
    "row_count": {
        "family": "record set",
        "verifies": ("the record count fits the bounds. With no bounds given, the artifact "
                     "must simply not be empty"),
        "fields": {"target": f"ref or path to {RECORD_SOURCE}",
                   "equals": "exact count", "min": "lower bound", "max": "upper bound",
                   "records_key": "optional array name inside a JSON object",
                   "sheet": "optional Excel sheet name or 1-based number",
                   "header_row": "optional 1-based Excel header row, default 1"},
    },
    "unique_key": {
        "family": "record set",
        "verifies": "no duplicate keys, and no record missing the key",
        "fields": {"target": f"ref or path to {RECORD_SOURCE}",
                   "key": "key column, default id",
                   "records_key": "optional array name inside a JSON object",
                   "sheet": "optional Excel sheet name or 1-based number",
                   "header_row": "optional 1-based Excel header row, default 1"},
    },
    "subset_of": {
        "family": "record set",
        "verifies": ("every key in the output exists in the source. This is the check for "
                     "invented records: an agent that adds a plausible row that was never "
                     "in the input fails here"),
        "fields": {"target": "ref or path to the artifact under test",
                   "source": "ref or path to the record set the keys must come from",
                   "key": "key column in the output, default id",
                   "source_key": "key column in the source, defaults to key",
                   "records_key": "optional array name inside the target",
                   "source_records_key": "optional array name inside the source",
                   "sheet": "optional Excel sheet for the target",
                   "source_sheet": "optional Excel sheet for the source"},
    },
    "numeric_range": {
        "family": "record set",
        "verifies": ("values stay within bounds. Numbers are compared in integer minor "
                     "units, so there is no floating point and precision is two decimals"),
        "fields": {"target": f"ref or path to {RECORD_SOURCE}",
                   "column": "the numeric column (not needed when aggregate is count)",
                   "aggregate": "each (default), sum, min, max or count",
                   "min": "lower bound", "max": "upper bound",
                   "records_key": "optional array name inside a JSON object",
                   "sheet": "optional Excel sheet name or 1-based number",
                   "header_row": "optional 1-based Excel header row, default 1"},
    },
    "reconcile_rows": {
        "family": "record set",
        "verifies": ("every key present in one input but not the other appears exactly once "
                     "in the output. The expected set is recomputed from the inputs, so the "
                     "output is never trusted as the source of truth"),
        "fields": {"orders": "ref or path to the full record set",
                   "payments": "ref or path to the set that cancels records out",
                   "output": "ref or path to the artifact under test",
                   "key": "join column, default order_id",
                   "orders_sheet": "optional Excel sheet for orders",
                   "payments_sheet": "optional Excel sheet for payments",
                   "output_sheet": "optional Excel sheet for the output"},
    },
    "amount_sum": {
        "family": "record set",
        "verifies": ("the output total equals the total recomputed from the inputs, summed "
                     "in integer minor units"),
        "fields": {"orders": "ref or path to the full record set",
                   "payments": "ref or path to the set that cancels records out",
                   "output": "ref or path to the artifact under test",
                   "key": "join column, default order_id",
                   "amount_column": "amount column, default amount"},
    },
    "json_valid": {
        "family": "JSON document",
        "verifies": ("the file parses as JSON, and optionally that the top level is the "
                     "expected kind. Catches prose, a markdown fence or a truncated write"),
        "fields": {"target": "ref or path to the JSON file",
                   "top_level": "optional: object, array, string, number, boolean or null"},
    },
    "json_keys": {
        "family": "JSON document",
        "verifies": ("required paths are present, and listed paths are not empty. Paths are "
                     "dotted, and a * step visits every element of an array, as in "
                     "items.*.order_id. An empty array does not satisfy a required path"),
        "fields": {"target": "ref or path to the JSON file",
                   "required": "dotted paths that must exist",
                   "non_empty": "dotted paths whose values must not be null, \"\", [] or {}"},
    },
    "text_contains": {
        "family": "text deliverable",
        "verifies": "required strings appear and forbidden strings do not",
        "fields": {"target": "ref or path to the text file",
                   "contains": "strings that must appear",
                   "absent": "strings that must not appear",
                   "case_sensitive": "default false"},
    },
    "no_placeholders": {
        "family": "text deliverable",
        "verifies": ("no placeholder or refusal text was left in the deliverable. Defaults "
                     f"to {DEFAULT_PLACEHOLDERS}"),
        "fields": {"target": "ref or path to the text file",
                   "patterns": "replace the default list",
                   "extra_patterns": "add to the default list"},
    },
}


# ---------- checks: traces ----------

def _trace_records(ctx, args) -> list:
    """Concatenate one or more trace files, in the order given."""
    refs = args.get("target")
    if isinstance(refs, str):
        refs = [refs]
    if not refs:
        raise CheckFail("target is required: one trace file or a list of them")

    def records_loader(path, records_key=None):
        return load_records(path, records_key=records_key, sheet=args.get("sheet"),
                            header_row=args.get("header_row", 1))

    records = []
    for ref in refs:
        path = ctx["resolve"](ref)
        if not path.is_file():
            raise CheckFail(f"trace file not found: {ctx['rel'](path)}")
        try:
            records += trace_mod.load_trace(path, args.get("line_prefix"),
                                            args.get("records_key"), records_loader)
        except (trace_mod.TraceError, ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise CheckFail(str(exc))
    return records


def _trace_check(fn):
    def check(ctx, args) -> dict:
        records = _trace_records(ctx, args)
        try:
            out = fn(records, args)
        except trace_mod.TraceError as exc:
            raise CheckFail(str(exc))
        out["steps_in_trace"] = len(records)
        return out
    return check


for _name, _fn in trace_mod.TRACE_CHECKS.items():
    CHECKS[_name] = _trace_check(_fn)
CHECK_DOCS.update(trace_mod.TRACE_DOCS)


# ---------- run ----------

def resolve_ref(spec_dir: Path, spec: dict, ref: str) -> Path:
    """Accept either "inputs.orders" or a plain relative path like "out/result.csv"."""
    if "." in ref:
        group, _, name = ref.partition(".")
        if group in ("inputs", "outputs") and name in spec.get(group, {}):
            return (spec_dir / spec[group][name]).resolve()
    return (spec_dir / ref).resolve()


def run(spec_path: Path, evidence_root: Path) -> dict:
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    spec_dir = spec_path.parent.resolve()

    def rel(p) -> str:
        p = Path(p)
        try:
            return p.relative_to(spec_dir).as_posix()
        except ValueError:
            return p.as_posix()

    ctx = {"resolve": lambda ref: resolve_ref(spec_dir, spec, ref), "rel": rel}

    def file_record(ref: str) -> dict:
        p = ctx["resolve"](ref)
        if not p.is_file():
            return {"path": rel(p), "exists": False}
        return {"path": rel(p), "exists": True,
                "bytes": p.stat().st_size, "sha256": sha256_file(p)}

    inputs = {name: file_record(f"inputs.{name}") for name in spec.get("inputs", {})}
    outputs = {name: file_record(f"outputs.{name}") for name in spec.get("outputs", {})}

    results = []
    for item in spec.get("checks", []):
        kind = item.get("type")
        name = item.get("name", kind or "unnamed")
        fn = CHECKS.get(kind)
        if fn is None:
            results.append({"name": name, "type": kind, "status": "fail",
                            "detail": f"unknown check type: {kind}"})
            continue
        try:
            out = fn(ctx, item)
        except CheckFail as exc:
            out = {"status": "fail", "detail": str(exc)}
        except KeyError as exc:
            out = {"status": "fail", "detail": f"spec is missing field {exc}"}
        except (OSError, ValueError, TypeError, UnicodeDecodeError) as exc:
            out = {"status": "fail", "detail": f"{type(exc).__name__}: {exc}"}
        out.update({"name": name, "type": kind})
        results.append(out)

    core = spec.get("core_output")
    core_ok = outputs.get(core, {}).get("exists", False) if core else True
    passed = sum(1 for r in results if r["status"] == "pass")
    if results and passed == len(results):
        verdict = "pass"
    elif core_ok and passed > 0:
        verdict = "partial"
    else:
        verdict = "fail"

    stable = {
        "tool": "taskattest",
        "tool_version": TOOL_VERSION,
        "task_id": spec.get("task_id"),
        "spec_sha256": sha256_file(spec_path),
        "inputs": inputs,
        "outputs": outputs,
        "checks": [{k: v for k, v in r.items() if k != "detail"} for r in results],
        "verdict": verdict,
    }
    digest = canonical_digest(stable)

    manifest = dict(stable)
    manifest["check_details"] = [{"name": r["name"], "status": r["status"],
                                  "detail": r.get("detail", "")} for r in results]
    manifest["evidence_digest"] = digest
    manifest["finished_at_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    manifest["replay_command"] = f'python -m taskattest --spec "{spec_path}"'
    manifest["spec_path"] = str(spec_path.resolve())
    manifest["judged_by"] = "deterministic_rules_only"
    manifest["model_calls"] = 0

    out_dir = evidence_root / str(spec.get("task_id", "task")) / digest[:12]
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest["evidence_dir"] = str(out_dir)
    return manifest


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="taskattest - verify agent task completion from the files it produced")
    ap.add_argument("--spec", required=True, help="path to the task definition JSON")
    ap.add_argument("--evidence-dir", default=None, help="where to write evidence bundles")
    ap.add_argument("--quiet", action="store_true", help="print nothing, use the exit code")
    a = ap.parse_args(argv)

    spec_path = Path(a.spec).resolve()
    evidence_root = Path(a.evidence_dir).resolve() if a.evidence_dir else spec_path.parent / "evidence"
    m = run(spec_path, evidence_root)

    if not a.quiet:
        print(f"[{m['verdict'].upper()}] {m['task_id']}  (model calls: {m['model_calls']})")
        for r in m["check_details"]:
            print(f"  - {r['status']:7} {r['name']}: {r['detail']}")
        print(f"  evidence: {m['evidence_dir']}")
        print(f"  replay:   {m['replay_command']}")
    return 0 if m["verdict"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
