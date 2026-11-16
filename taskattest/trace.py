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

"""Trace checks: judge what an agent or a robot *did*, not only what it left behind.

A trace is a time-ordered list of state records: JSONL, a JSON array, CSV, or a
raw log in which only lines carrying a given prefix are records. Five checks:

  trace_always      a condition holds at every step (an invariant)
  trace_eventually  a condition holds at some step
  trace_final       the last step satisfies a condition
  trace_count       how many steps, transitions, or distinct values match
  trace_order       steps happen in a given order, optionally within each group

Conditions are data, never code. Nothing is evaluated: a condition is a small
JSON object read by a fixed interpreter. Numbers compare as decimals parsed from
their text, so the same trace gives the same verdict on every machine.
"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from pathlib import Path

COMPARE_OPS = ("eq", "ne", "lt", "le", "gt", "ge", "in", "startswith", "exists")


class TraceError(Exception):
    """A trace that cannot be read, or a condition that cannot be understood."""


# ---------- reading ----------

def _decode(path: Path) -> str:
    raw = path.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    return raw.decode("utf-8-sig")


def load_trace(path: Path, line_prefix=None, records_key=None, load_records=None) -> list:
    """Read one trace file as a list of dict records, in file order.

    With `line_prefix`, the file is treated as a log: only lines starting with
    the prefix are records, and the rest of the line must be one JSON object.
    UTF-16 logs (as PowerShell writes them) are detected by their byte order mark.
    """
    path = Path(path)
    if line_prefix:
        records = []
        for number, line in enumerate(_decode(path).splitlines(), start=1):
            if not line.startswith(line_prefix):
                continue
            body = line[len(line_prefix):].strip()
            try:
                record = json.loads(body)
            except json.JSONDecodeError as exc:
                raise TraceError(f"{path.name} line {number}: not JSON after the prefix ({exc.msg})")
            if isinstance(record, dict):
                records.append(record)
        return records
    if path.suffix.lower() == ".jsonl":
        records = []
        for number, line in enumerate(_decode(path).splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise TraceError(f"{path.name} line {number}: not JSON ({exc.msg})")
            if isinstance(record, dict):
                records.append(record)
        return records
    if load_records is None:
        raise TraceError("no record loader available for this file type")
    return load_records(path, records_key=records_key)


# ---------- conditions ----------

_MISSING = object()


def lookup(record, dotted):
    node = record
    for part in str(dotted).split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return _MISSING
    return node


def _number(value):
    if isinstance(value, bool) or value is None or value is _MISSING:
        return None
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None


def _equal(a, b) -> bool:
    na, nb = _number(a), _number(b)
    if na is not None and nb is not None:
        return na == nb
    return a == b


def validate(cond, where="condition") -> None:
    """Reject a malformed condition before any record is read, so a typo fails loudly."""
    if not isinstance(cond, dict) or not cond:
        raise TraceError(f"{where} must be a non-empty object")
    if "all" in cond or "any" in cond:
        key = "all" if "all" in cond else "any"
        items = cond[key]
        if not isinstance(items, list) or not items:
            raise TraceError(f"{where}.{key} must be a non-empty list")
        for i, item in enumerate(items):
            validate(item, f"{where}.{key}[{i}]")
        return
    if "not" in cond:
        validate(cond["not"], f"{where}.not")
        return
    if "within" in cond:
        w = cond["within"]
        if not isinstance(w, dict) or not {"fields", "point", "radius"} <= set(w):
            raise TraceError(f"{where}.within needs fields, point and radius")
        if len(w["fields"]) != len(w["point"]) or not w["fields"]:
            raise TraceError(f"{where}.within: fields and point must have the same length")
        if _number(w["radius"]) is None:
            raise TraceError(f"{where}.within.radius must be a number")
        return
    if "field" not in cond:
        raise TraceError(f"{where} needs a field, or one of all / any / not / within")
    ops = [op for op in COMPARE_OPS if op in cond]
    unknown = [k for k in cond if k not in COMPARE_OPS and k != "field"]
    if unknown:
        raise TraceError(f"{where}: unknown operator {unknown[0]!r}; use one of {list(COMPARE_OPS)}")
    if len(ops) != 1:
        raise TraceError(f"{where} needs exactly one operator, found {ops or 'none'}")
    if ops[0] in ("lt", "le", "gt", "ge") and _number(cond[ops[0]]) is None:
        raise TraceError(f"{where}.{ops[0]} must be a number")
    if ops[0] == "in" and not isinstance(cond["in"], list):
        raise TraceError(f"{where}.in must be a list")


def holds(cond, record) -> bool:
    """Evaluate a validated condition. A missing field never satisfies a comparison."""
    if "all" in cond:
        return all(holds(c, record) for c in cond["all"])
    if "any" in cond:
        return any(holds(c, record) for c in cond["any"])
    if "not" in cond:
        return not holds(cond["not"], record)
    if "within" in cond:
        w = cond["within"]
        total = Decimal(0)
        for field, centre in zip(w["fields"], w["point"]):
            value, centre = _number(lookup(record, field)), _number(centre)
            if value is None or centre is None:
                return False
            total += (value - centre) ** 2
        radius = _number(w["radius"])
        return total <= radius * radius  # squared, so no square root is ever taken

    value = lookup(record, cond["field"])
    if "exists" in cond:
        return (value is not _MISSING) == bool(cond["exists"])
    if value is _MISSING:
        return False
    if "eq" in cond:
        return _equal(value, cond["eq"])
    if "ne" in cond:
        return not _equal(value, cond["ne"])
    if "in" in cond:
        return any(_equal(value, option) for option in cond["in"])
    if "startswith" in cond:
        return isinstance(value, str) and value.startswith(str(cond["startswith"]))
    number, bound = _number(value), _number(next(cond[op] for op in ("lt", "le", "gt", "ge") if op in cond))
    if number is None:
        return False
    if "lt" in cond:
        return number < bound
    if "le" in cond:
        return number <= bound
    if "gt" in cond:
        return number > bound
    return number >= bound


# ---------- checks ----------

def _bounds(count: int, args: dict) -> list:
    equals, low, high = args.get("equals"), args.get("min"), args.get("max")
    if equals is None and low is None and high is None:
        low = 1
    problems = []
    if equals is not None and count != int(equals):
        problems.append(f"expected exactly {int(equals)}")
    if low is not None and count < int(low):
        problems.append(f"expected at least {int(low)}")
    if high is not None and count > int(high):
        problems.append(f"expected at most {int(high)}")
    return problems


def check_trace_always(records: list, args: dict) -> dict:
    cond = args.get("condition")
    validate(cond)
    when = args.get("when")
    if when is not None:
        validate(when, "when")
    checked = 0
    violations = []
    for index, record in enumerate(records):
        if when is not None and not holds(when, record):
            continue
        checked += 1
        if not holds(cond, record):
            violations.append(index)
    if checked == 0:
        return {"status": "fail", "detail": "no step was checked, so nothing was proven",
                "steps_checked": 0, "violations": 0}
    ok = not violations
    detail = f"{checked} step(s) checked, {len(violations)} violation(s)"
    if violations:
        detail += f", first at step {violations[0]}"
    return {"status": "pass" if ok else "fail", "detail": detail,
            "steps_checked": checked, "violations": len(violations),
            "first_violation": violations[0] if violations else None}


def check_trace_eventually(records: list, args: dict) -> dict:
    cond = args.get("condition")
    validate(cond)
    first = next((i for i, r in enumerate(records) if holds(cond, r)), None)
    if first is None:
        return {"status": "fail", "detail": f"never held across {len(records)} step(s)",
                "first_match": None}
    return {"status": "pass", "detail": f"first held at step {first} of {len(records)}",
            "first_match": first}


def check_trace_final(records: list, args: dict) -> dict:
    cond = args.get("condition")
    validate(cond)
    when = args.get("when")
    if when is not None:
        validate(when, "when")
    candidates = [i for i, r in enumerate(records) if when is None or holds(when, r)]
    if not candidates:
        return {"status": "fail", "detail": "no step to take as final", "final_step": None}
    last = candidates[-1]
    ok = holds(cond, records[last])
    return {"status": "pass" if ok else "fail",
            "detail": f"final step {last} {'satisfies' if ok else 'does not satisfy'} the condition",
            "final_step": last}


def check_trace_count(records: list, args: dict) -> dict:
    cond = args.get("where")
    validate(cond, "where")
    matches = [holds(cond, r) for r in records]
    distinct = args.get("distinct")
    if distinct:
        values = []
        for record, matched in zip(records, matches):
            if matched:
                value = lookup(record, distinct)
                key = json.dumps(value, sort_keys=True) if value is not _MISSING else None
                if key is not None and key not in values:
                    values.append(key)
        count, what = len(values), f"distinct {distinct}"
    elif args.get("edges"):
        count = sum(1 for i, m in enumerate(matches) if m and (i == 0 or not matches[i - 1]))
        what = "transition(s) into the condition"
    else:
        count, what = sum(matches), "matching step(s)"
    problems = _bounds(count, args)
    return {"status": "pass" if not problems else "fail",
            "detail": f"{count} {what}" + ("; " + "; ".join(problems) if problems else ""),
            "count": count}


def check_trace_order(records: list, args: dict) -> dict:
    steps = args.get("steps")
    if not isinstance(steps, list) or len(steps) < 2:
        raise TraceError("trace_order needs a steps list of at least two conditions")
    for i, step in enumerate(steps):
        validate(step, f"steps[{i}]")
    group_by = args.get("group_by")

    groups: dict = {}
    for record in records:
        if group_by:
            value = lookup(record, group_by)
            if value is _MISSING:
                continue
            key = json.dumps(value, sort_keys=True)
        else:
            key = "all"
        groups.setdefault(key, []).append(record)

    def completions(seq) -> int:
        done, position = 0, 0
        for record in seq:
            if holds(steps[position], record):
                position += 1
                if position == len(steps):
                    done, position = done + 1, 0
        return done

    if group_by:
        complete = [k for k, seq in groups.items() if completions(seq) >= 1]
        incomplete = [json.loads(k) for k in groups if k not in complete]
        total = len(groups)
        need = args.get("min_groups")
        need = total if need is None else int(need)
        ok = total > 0 and len(complete) >= need
        detail = (f"{len(complete)} of {total} {group_by} group(s) ran all {len(steps)} steps in order, "
                  f"needed {need}")
        if incomplete:
            shown = ", ".join(str(v) for v in incomplete[:8])
            detail += f"; incomplete: {shown}" + (" ..." if len(incomplete) > 8 else "")
        return {"status": "pass" if ok else "fail", "detail": detail,
                "groups": total, "groups_complete": len(complete),
                "incomplete_groups": incomplete[:50]}

    repeats = completions(groups.get("all", []))
    need = int(args.get("min_repeats", 1))
    return {"status": "pass" if repeats >= need else "fail",
            "detail": f"the {len(steps)}-step sequence completed {repeats} time(s), needed {need}",
            "repeats": repeats}


TRACE_CHECKS = {
    "trace_always": check_trace_always,
    "trace_eventually": check_trace_eventually,
    "trace_final": check_trace_final,
    "trace_count": check_trace_count,
    "trace_order": check_trace_order,
}

CONDITION_HELP = (
    "A condition is JSON, never code: {\"field\": \"phase\", \"eq\": \"placed\"} with one of "
    "eq, ne, lt, le, gt, ge, in, startswith, exists; or {\"all\": [...]}, {\"any\": [...]}, "
    "{\"not\": {...}}; or {\"within\": {\"fields\": [\"pos.x\", \"pos.z\"], \"point\": [1, 2], "
    "\"radius\": 0.04}} for a distance. Fields are dotted paths. Numbers compare as decimals."
)

TRACE_SOURCE = ("one trace file or a list of them, read in order: JSONL, a JSON array, CSV, or a raw "
                "log with line_prefix so only lines starting with it count as records")

TRACE_DOCS = {
    "trace_always": {
        "family": "trace",
        "verifies": ("an invariant: the condition holds at every step, or at every step where "
                     "`when` holds. Fails if no step was checked. " + CONDITION_HELP),
        "fields": {"target": TRACE_SOURCE, "condition": "the invariant",
                   "when": "optional: only check steps where this holds",
                   "line_prefix": "optional: treat the file as a log"},
    },
    "trace_eventually": {
        "family": "trace",
        "verifies": "the condition holds at some step. " + CONDITION_HELP,
        "fields": {"target": TRACE_SOURCE, "condition": "what must happen at least once",
                   "line_prefix": "optional"},
    },
    "trace_final": {
        "family": "trace",
        "verifies": ("the last step, or the last step where `when` holds, satisfies the "
                     "condition. " + CONDITION_HELP),
        "fields": {"target": TRACE_SOURCE, "condition": "the required end state",
                   "when": "optional", "line_prefix": "optional"},
    },
    "trace_count": {
        "family": "trace",
        "verifies": ("how many steps match `where`, or with edges=true how many times the trace "
                     "entered that state, or with distinct=<field> how many different values of "
                     "that field occur among matching steps. Bounds equals / min / max; with none, "
                     "at least 1. " + CONDITION_HELP),
        "fields": {"target": TRACE_SOURCE, "where": "the condition to count",
                   "edges": "optional bool", "distinct": "optional field",
                   "equals": "exact", "min": "lower bound", "max": "upper bound",
                   "line_prefix": "optional"},
    },
    "trace_order": {
        "family": "trace",
        "verifies": ("the steps happen in the given order. With group_by=<field>, the order is "
                     "checked inside each group (for example each job or retry loop) and min_groups "
                     "of them must complete, all by default. Without it, the whole sequence must "
                     "complete min_repeats times, once by default. " + CONDITION_HELP),
        "fields": {"target": TRACE_SOURCE, "steps": "list of at least two conditions",
                   "group_by": "optional field", "min_groups": "optional",
                   "min_repeats": "optional", "line_prefix": "optional"},
    },
}
