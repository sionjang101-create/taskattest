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

"""Trace tests: the condition language, then a multi-step job judged from a raw log.

The log is written the way many runners write theirs: UTF-16 with a byte order
mark, one "EVT {json}" line per state change, and other lines mixed in that are
not records. A clean run must pass every trace check; a run with a stalled job,
a step done out of order, and a moment over budget must fail exactly the checks
aimed at those flaws.
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
PHASES = ["fetch", "parse", "validate", "write", "commit", "report"]

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
    r = {"phase": "validate", "job": 3, "count": "7", "pos": {"x": 1.0, "z": 2.0, "y": 0.25},
         "tags": ["a", "b"]}
    check(tr.holds({"field": "count", "eq": 7}, r), 'text "7" equals number 7')
    check(tr.holds({"field": "pos.y", "ge": "0.25"}, r), "0.25 >= 0.25 compares exactly, as decimals")
    check(not tr.holds({"field": "missing", "lt": 10}, r), "a missing field never satisfies a comparison")
    check(not tr.holds({"field": "missing", "ne": 1}, r), "not even ne: absence is not a value")
    check(tr.holds({"field": "missing", "exists": False}, r), "exists: false holds for an absent field")
    check(tr.holds({"field": "tags.1", "eq": "b"}, r), "dotted paths index into lists")
    check(tr.holds({"field": "phase", "in": ["validate", "parse"]}, r), "in")
    check(tr.holds({"field": "phase", "startswith": "valid"}, r), "startswith")
    check(tr.holds({"all": [{"field": "job", "gt": 2}, {"not": {"field": "phase", "eq": "fetch"}}]}, r),
          "all + not")
    check(tr.holds({"any": [{"field": "job", "eq": 9}, {"field": "job", "eq": 3}]}, r), "any")
    edge = {"within": {"fields": ["pos.x", "pos.z"], "point": [1.03, 2.04], "radius": 0.05}}
    check(tr.holds(edge, r), "within: exactly on the radius counts as inside (3-4-5, no square root)")
    check(not tr.holds({"within": {"fields": ["pos.x", "pos.z"], "point": [1.03, 2.04],
                                   "radius": "0.0499"}}, r), "within: just short of it does not")
    raises(lambda: tr.validate({"field": "phase", "equals": "x"}), "unknown operator is refused up front")
    raises(lambda: tr.validate({"field": "phase", "eq": 1, "ne": 2}), "two operators are refused")
    raises(lambda: tr.validate({"field": "job", "lt": "soon"}), "a non-numeric bound is refused")
    raises(lambda: tr.validate({"within": {"fields": ["a"], "point": [1, 2], "radius": 1}}),
           "within with mismatched lengths is refused")


# ---------- part two: a multi-step job judged from a raw log ----------

def write_log(path: Path, jobs: dict) -> None:
    """jobs: {job: [(phase, budget), ...]} written as UTF-16 with noise lines."""
    lines = ["runner started", "loading config"]
    t = 0.0
    committed = 0
    for job, steps in jobs.items():
        for phase, budget in steps:
            t += 1.5
            if phase == "commit":
                committed += 1
            record = {"job": job, "phase": phase, "time": t, "committed": committed,
                      "budget": {"remaining": budget}}
            lines.append("EVT " + json.dumps(record))
        lines.append(f"EVT_SUMMARY {json.dumps({'job': job, 'status': 'job_end'})}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(("\r\n".join(lines) + "\r\n").encode("utf-16"))


def build_case(name: str, flawed: bool) -> Path:
    case = WORK / name
    if case.exists():
        shutil.rmtree(case)
    good = [(p, 0.42) for p in PHASES]
    jobs = {j: list(good) for j in range(6)}
    if flawed:
        jobs[3] = [(p, 0.42) for p in PHASES[:4]]          # stalls after write: never commits
        jobs[1] = [("parse", 0.42)] + [(p, 0.42) for p in PHASES if p != "parse"]
        #           ^ parses before fetching: step order broken
        jobs[4] = [(p, (-0.30 if p == "parse" else 0.42)) for p in PHASES]  # over budget
    write_log(case / "logs" / "session01.log", {k: v for k, v in jobs.items() if k < 3})
    write_log(case / "logs" / "session02.log", {k: v for k, v in jobs.items() if k >= 3})

    spec = {
        "task_id": "agent_jobs",
        "description": "Six jobs, each fetch -> parse -> validate -> write -> commit -> report, "
                       "judged from the runner's raw logs.",
        "inputs": {"log1": "logs/session01.log", "log2": "logs/session02.log"},
        "outputs": {},
        "checks": [
            {"name": "every job runs its steps in order", "type": "trace_order",
             "target": ["inputs.log1", "inputs.log2"], "line_prefix": "EVT ",
             "group_by": "job",
             "steps": [{"field": "phase", "eq": p} for p in PHASES]},
            {"name": "at least 5 of 6 jobs committed", "type": "trace_count",
             "target": ["inputs.log1", "inputs.log2"], "line_prefix": "EVT ",
             "where": {"field": "phase", "eq": "commit"}, "distinct": "job", "min": 5},
            {"name": "never over budget", "type": "trace_always",
             "target": ["inputs.log1", "inputs.log2"], "line_prefix": "EVT ",
             "condition": {"field": "budget.remaining", "ge": 0}},
            {"name": "the run ends by reporting", "type": "trace_final",
             "target": ["inputs.log1", "inputs.log2"], "line_prefix": "EVT ",
             "condition": {"field": "phase", "eq": "report"}},
            {"name": "a job was committed at some point", "type": "trace_eventually",
             "target": ["inputs.log1", "inputs.log2"], "line_prefix": "EVT ",
             "condition": {"field": "phase", "eq": "commit"}},
            {"name": "every job was parsed", "type": "trace_count",
             "target": ["inputs.log1", "inputs.log2"], "line_prefix": "EVT ",
             "where": {"field": "phase", "eq": "parse"}, "distinct": "job", "equals": 6},
        ],
    }
    (case / "spec.json").write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
    return case / "spec.json"


def test_jobs() -> None:
    print("\n=== multi-step jobs from a raw UTF-16 log ===")
    ok_spec = build_case("trace_ok", flawed=False)
    first = runner.run(ok_spec, ok_spec.parent / "evidence")
    second = runner.run(ok_spec, ok_spec.parent / "evidence")
    check(first["verdict"] == "pass", f"clean run: {first['verdict']}")
    check(first["evidence_digest"] == second["evidence_digest"],
          f"digest stable {first['evidence_digest'][:12]}")
    records = [c for c in first["checks"] if c["type"] == "trace_order"][0]
    check(records["steps_in_trace"] == 36, f"only EVT lines are records: {records['steps_in_trace']} of 36")
    for d in first["check_details"]:
        print(f"       {d['status']:4} {d['name']}: {d['detail']}")

    bad_spec = build_case("trace_flawed", flawed=True)
    bad = runner.run(bad_spec, bad_spec.parent / "evidence")
    check(bad["verdict"] == "partial", f"flawed run: {bad['verdict']}")
    failed = {d["name"] for d in bad["check_details"] if d["status"] == "fail"}
    expected = {"every job runs its steps in order", "never over budget"}
    check(failed == expected, f"exactly the order and budget checks fail: {sorted(failed)}")
    order = [c for c in bad["checks"] if c["type"] == "trace_order"][0]
    check(sorted(order["incomplete_groups"]) == [1, 3],
          f"incomplete jobs named: {order['incomplete_groups']}")
    budget = [c for c in bad["checks"] if c["name"] == "never over budget"][0]
    check(budget["violations"] == 1, f"one step over budget: {budget['violations']}")
    for d in bad["check_details"]:
        print(f"       {d['status']:4} {d['name']}: {d['detail']}")
    committed = [c for c in bad["checks"] if c["name"] == "at least 5 of 6 jobs committed"][0]
    check(committed["count"] == 5 and committed["status"] == "pass",
          f"5 of 6 committed still meets a threshold of 5: count {committed['count']}")


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
    test_jobs()
    test_fail_closed()
    test_root_guard()
    print("\n=== summary ===")
    if failures:
        for f in failures:
            print("FAILED:", f)
        return 1
    print("trace: conditions, multi-step jobs from a raw log, fail-closed cases, root confinement")
    return 0


if __name__ == "__main__":
    sys.exit(main())
