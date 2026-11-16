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

"""Trace tests: the condition language, then a robot delivery judged from a raw log.

The log is written the way a real warehouse-robot runner wrote ours: UTF-16 with
a byte order mark, one "CONT {json}" line per state change, and other lines mixed
in that are not records. A clean run must pass every trace check; a run with a
stalled delivery, a phase skipped, and a robot that dropped below the floor must
fail exactly the checks aimed at those flaws.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from taskattest import mcp_server, runner
from taskattest import trace as tr

ROOT = Path(__file__).parent.resolve()
WORK = ROOT / "fixtures"
PHASES = ["pickup", "clearing_support", "navigation", "docking", "placement_release", "returning"]
PALLETS = ["P04", "P08", "P12"]

failures: list = []


def check(condition: bool, label: str) -> None:
    print(("OK  " if condition else "NG  ") + label)
    if not condition:
        failures.append(label)


def raises(fn, label: str) -> None:
    try:
        fn()
    except tr.TraceError as exc:
        check(True, f"{label}: {exc}")
        return
    check(False, f"{label}: no error raised")


# ---------- part one: the condition language ----------

def test_conditions() -> None:
    print("=== conditions ===")
    r = {"phase": "docking", "cycle": 3, "count": "7", "root": {"x": 1.0, "z": 2.0, "y": 0.25},
         "tags": ["a", "b"]}
    check(tr.holds({"field": "count", "eq": 7}, r), 'text "7" equals number 7')
    check(tr.holds({"field": "root.y", "ge": "0.25"}, r), "0.25 >= 0.25 compares exactly, as decimals")
    check(not tr.holds({"field": "missing", "lt": 10}, r), "a missing field never satisfies a comparison")
    check(not tr.holds({"field": "missing", "ne": 1}, r), "not even ne: absence is not a value")
    check(tr.holds({"field": "missing", "exists": False}, r), "exists: false holds for an absent field")
    check(tr.holds({"field": "tags.1", "eq": "b"}, r), "dotted paths index into lists")
    check(tr.holds({"field": "phase", "in": ["docking", "navigation"]}, r), "in")
    check(tr.holds({"field": "phase", "startswith": "dock"}, r), "startswith")
    check(tr.holds({"all": [{"field": "cycle", "gt": 2}, {"not": {"field": "phase", "eq": "pickup"}}]}, r),
          "all + not")
    check(tr.holds({"any": [{"field": "cycle", "eq": 9}, {"field": "cycle", "eq": 3}]}, r), "any")
    edge = {"within": {"fields": ["root.x", "root.z"], "point": [1.03, 2.04], "radius": 0.05}}
    check(tr.holds(edge, r), "within: exactly on the radius counts as inside (3-4-5, no square root)")
    check(not tr.holds({"within": {"fields": ["root.x", "root.z"], "point": [1.03, 2.04],
                                   "radius": "0.0499"}}, r), "within: just short of it does not")
    raises(lambda: tr.validate({"field": "phase", "equals": "x"}), "unknown operator is refused up front")
    raises(lambda: tr.validate({"field": "phase", "eq": 1, "ne": 2}), "two operators are refused")
    raises(lambda: tr.validate({"field": "cycle", "lt": "soon"}), "a non-numeric bound is refused")
    raises(lambda: tr.validate({"within": {"fields": ["a"], "point": [1, 2], "radius": 1}}),
           "within with mismatched lengths is refused")


# ---------- part two: a robot delivery judged from a raw log ----------

def write_log(path: Path, cycles: dict) -> None:
    """cycles: {cycle: [(phase, y), ...]} written as UTF-16 with noise lines."""
    lines = ["Runner started", "loading scene"]
    t = 0.0
    deliveries = 0
    for cycle, steps in cycles.items():
        for phase, y in steps:
            t += 1.5
            if phase == "placement_release":
                deliveries += 1
            record = {"cycle": cycle, "phase": phase, "time": t, "deliveries": deliveries,
                      "root": {"x": 17.0 + cycle * 0.1, "y": y, "z": 7.0}}
            lines.append("CONT " + json.dumps(record))
        lines.append(f"CONT_RESULT {json.dumps({'cycle': cycle, 'status': 'cycle_end'})}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(("\r\n".join(lines) + "\r\n").encode("utf-16"))


def build_case(name: str, flawed: bool) -> Path:
    case = WORK / name
    if case.exists():
        shutil.rmtree(case)
    good = [(p, 0.42) for p in PHASES]
    cycles = {c: list(good) for c in range(6)}
    if flawed:
        cycles[3] = [(p, 0.42) for p in PHASES[:4]]          # stalls at docking: never places
        cycles[1] = [("clearing_support", 0.42)] + [(p, 0.42) for p in PHASES if p != "clearing_support"]
        #             ^ lifts before picking up: phase order broken
        cycles[4] = [(p, (-0.30 if p == "navigation" else 0.42)) for p in PHASES]  # below the floor
    write_log(case / "logs" / "session01.log", {k: v for k, v in cycles.items() if k < 3})
    write_log(case / "logs" / "session02.log", {k: v for k, v in cycles.items() if k >= 3})

    spec = {
        "task_id": "robot_delivery",
        "description": "Six deliveries P04 -> P08 -> P12 -> ..., judged from the runner's raw logs.",
        "inputs": {"log1": "logs/session01.log", "log2": "logs/session02.log"},
        "outputs": {},
        "checks": [
            {"name": "every cycle runs its phases in order", "type": "trace_order",
             "target": ["inputs.log1", "inputs.log2"], "line_prefix": "CONT ",
             "group_by": "cycle",
             "steps": [{"field": "phase", "eq": p} for p in PHASES]},
            {"name": "at least 5 of 6 boxes placed", "type": "trace_count",
             "target": ["inputs.log1", "inputs.log2"], "line_prefix": "CONT ",
             "where": {"field": "phase", "eq": "placement_release"}, "distinct": "cycle", "min": 5},
            {"name": "never below the floor", "type": "trace_always",
             "target": ["inputs.log1", "inputs.log2"], "line_prefix": "CONT ",
             "condition": {"field": "root.y", "ge": 0}},
            {"name": "the run ends by returning", "type": "trace_final",
             "target": ["inputs.log1", "inputs.log2"], "line_prefix": "CONT ",
             "condition": {"field": "phase", "eq": "returning"}},
            {"name": "a box was placed at some point", "type": "trace_eventually",
             "target": ["inputs.log1", "inputs.log2"], "line_prefix": "CONT ",
             "condition": {"field": "phase", "eq": "placement_release"}},
            {"name": "the next box was picked after every placement", "type": "trace_count",
             "target": ["inputs.log1", "inputs.log2"], "line_prefix": "CONT ",
             "where": {"field": "phase", "eq": "clearing_support"}, "distinct": "cycle", "equals": 6},
        ],
    }
    (case / "spec.json").write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
    return case / "spec.json"


def test_robot() -> None:
    print("\n=== robot delivery from a raw UTF-16 log ===")
    ok_spec = build_case("trace_ok", flawed=False)
    first = runner.run(ok_spec, ok_spec.parent / "evidence")
    second = runner.run(ok_spec, ok_spec.parent / "evidence")
    check(first["verdict"] == "pass", f"clean run: {first['verdict']}")
    check(first["evidence_digest"] == second["evidence_digest"],
          f"digest stable {first['evidence_digest'][:12]}")
    records = [c for c in first["checks"] if c["type"] == "trace_order"][0]
    check(records["steps_in_trace"] == 36, f"only CONT lines are records: {records['steps_in_trace']} of 36")
    for d in first["check_details"]:
        print(f"       {d['status']:4} {d['name']}: {d['detail']}")

    bad_spec = build_case("trace_flawed", flawed=True)
    bad = runner.run(bad_spec, bad_spec.parent / "evidence")
    check(bad["verdict"] == "partial", f"flawed run: {bad['verdict']}")
    failed = {d["name"] for d in bad["check_details"] if d["status"] == "fail"}
    expected = {"every cycle runs its phases in order", "never below the floor"}
    check(failed == expected, f"exactly the order and floor checks fail: {sorted(failed)}")
    order = [c for c in bad["checks"] if c["type"] == "trace_order"][0]
    check(sorted(order["incomplete_groups"]) == [1, 3],
          f"incomplete cycles named: {order['incomplete_groups']}")
    floor = [c for c in bad["checks"] if c["name"] == "never below the floor"][0]
    check(floor["violations"] == 1, f"one step below the floor: {floor['violations']}")
    for d in bad["check_details"]:
        print(f"       {d['status']:4} {d['name']}: {d['detail']}")
    placed = [c for c in bad["checks"] if c["name"] == "at least 5 of 6 boxes placed"][0]
    check(placed["count"] == 5 and placed["status"] == "pass",
          f"5 of 6 placed still meets a threshold of 5: count {placed['count']}")


def test_fail_closed() -> None:
    print("\n=== fail closed ===")
    case = WORK / "trace_failclosed"
    if case.exists():
        shutil.rmtree(case)
    case.mkdir(parents=True)
    (case / "t.jsonl").write_text('{"phase": "a"}\n{"phase": "b"}\n', encoding="utf-8")
    spec = {"task_id": "fc", "inputs": {"t": "t.jsonl"}, "outputs": {}, "checks": [
        {"name": "invariant over nothing", "type": "trace_always", "target": "inputs.t",
         "condition": {"field": "phase", "ne": "x"}, "when": {"field": "phase", "eq": "never"}},
        {"name": "typo in operator", "type": "trace_eventually", "target": "inputs.t",
         "condition": {"field": "phase", "equals": "a"}},
        {"name": "missing trace", "type": "trace_count", "target": "nope.jsonl",
         "where": {"field": "phase", "eq": "a"}}]}
    (case / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
    m = runner.run(case / "spec.json", case / "evidence")
    statuses = {d["name"]: d["status"] for d in m["check_details"]}
    check(all(s == "fail" for s in statuses.values()), f"all three fail rather than pass: {statuses}")
    details = {d["name"]: d["detail"] for d in m["check_details"]}
    check("nothing was proven" in details["invariant over nothing"],
          "an invariant checked over zero steps proves nothing")
    check("unknown operator" in details["typo in operator"], "a typo is reported, not silently false")


def test_root_guard() -> None:
    print("\n=== --root confinement covers list targets and subset_of source ===")
    case = WORK / "trace_guard"
    if case.exists():
        shutil.rmtree(case)
    (case / "inside").mkdir(parents=True)
    (case / "inside" / "a.jsonl").write_text('{"k": 1}\n', encoding="utf-8")
    outside = WORK / "trace_guard_outside.jsonl"
    outside.write_text('{"k": 1}\n', encoding="utf-8")
    root = (case / "inside").resolve()
    for label, item in (
            ("list target", {"type": "trace_count", "target": ["a.jsonl", str(outside)],
                             "where": {"field": "k", "eq": 1}}),
            ("subset_of source", {"type": "subset_of", "target": "a.jsonl", "key": "k",
                                  "source": str(outside)})):
        spec_path = case / "inside" / f"spec_{label.replace(' ', '_')}.json"
        spec_path.write_text(json.dumps({"task_id": "g", "inputs": {}, "outputs": {},
                                         "checks": [item]}), encoding="utf-8")
        try:
            mcp_server.guard_spec_refs(spec_path.resolve(), root)
            check(False, f"{label}: a path outside --root was allowed")
        except mcp_server.ToolError as exc:
            check("outside the permitted root" in str(exc), f"{label}: refused")


def main() -> int:
    test_conditions()
    test_robot()
    test_fail_closed()
    test_root_guard()
    print("\n=== summary ===")
    if failures:
        for f in failures:
            print("FAILED:", f)
        return 1
    print("trace: conditions, a robot run from a raw log, fail-closed cases, root confinement")
    return 0


if __name__ == "__main__":
    sys.exit(main())
