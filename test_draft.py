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

"""Draft tests: what a draft proposes, and what it must refuse to do.

The property that matters most: a draft must never certify a wrong output by
fitting itself to it. So the tests seed a wrong output (a duplicated id) and
check the draft still proposes the check that catches it, check that no upper
bound is ever fitted, and check that a drafted spec keeps warning until a
person removes the flag.
"""

from __future__ import annotations

import csv
import json
import shutil
import subprocess
import sys
from pathlib import Path

from taskattest import draft, runner

ROOT = Path(__file__).parent.resolve()
WORK = ROOT / "fixtures" / "draft"
failures: list = []


def check(condition: bool, label: str) -> None:
    print(("OK  " if condition else "NG  ") + label)
    if not condition:
        failures.append(label)


def write_csv(path: Path, rows: list, fields: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def by_type(spec: dict, kind: str) -> list:
    return [c for c in spec["checks"] if c["type"] == kind]


def fresh(name: str) -> Path:
    case = WORK / name
    if case.exists():
        shutil.rmtree(case)
    case.mkdir(parents=True)
    return case


ORDERS = [{"order_id": "A-1", "sku": "S-10", "amount": "120"},
          {"order_id": "A-2", "sku": "S-10", "amount": "80"},
          {"order_id": "A-3", "sku": "S-20", "amount": "45"}]


def test_records() -> None:
    print("=== records: what is proposed ===")
    case = fresh("records")
    write_csv(case / "in" / "orders.csv", ORDERS, ["order_id", "sku", "amount"])
    write_csv(case / "out" / "picked.csv", ORDERS[:2], ["order_id", "sku", "amount"])
    r = draft.draft_spec(case, {"orders": case / "in/orders.csv"},
                         {"picked": case / "out/picked.csv"})
    spec = r["spec"]
    check(spec["draft"] is True, "the spec is marked draft")
    check(spec["inputs"]["orders"] == "in/orders.csv", "paths are relative to the spec")
    seal = by_type(spec, "file_hash")
    check(len(seal) == 1 and seal[0]["sha256"] == runner.sha256_file(case / "in/orders.csv"),
          "the input is sealed with its real sha256")
    check(by_type(spec, "csv_columns")[0]["columns"] == ["order_id", "sku", "amount"],
          "required columns are the ones the file has")
    unique = {c["key"] for c in by_type(spec, "unique_key")}
    check(unique == {"order_id"}, f"uniqueness only for the instance id, not for sku: {sorted(unique)}")
    related = {c["key"] for c in by_type(spec, "subset_of")}
    check(related == {"order_id", "sku"}, f"both ids are related back to the input: {sorted(related)}")
    ranges = by_type(spec, "numeric_range")
    check(len(ranges) == 1 and ranges[0]["column"] == "amount" and ranges[0]["min"] == 0,
          "an amount column gets a lower bound of 0")
    check(all("max" not in c for c in ranges), "no upper bound is ever fitted")
    path = draft.write_spec(spec, case / "spec.json")
    verdict = runner.run(path, case / "evidence")
    check(verdict["verdict"] == "pass", f"the honest output passes its draft: {verdict['verdict']}")
    check(len(verdict["warnings"]) == 1 and "unreviewed draft" in verdict["warnings"][0],
          "a verdict against a draft carries the warning")
    spec.pop("draft")
    (case / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
    check(runner.run(case / "spec.json", case / "evidence")["warnings"] == [],
          "removing the flag removes the warning")


def test_no_fitting() -> None:
    print("\n=== a draft must not certify a wrong output ===")
    case = fresh("wrong")
    write_csv(case / "in" / "orders.csv", ORDERS, ["order_id", "sku", "amount"])
    wrong = ORDERS[:2] + [dict(ORDERS[0]), {"order_id": "A-99", "sku": "S-10", "amount": "-5"}]
    write_csv(case / "out" / "picked.csv", wrong, ["order_id", "sku", "amount"])
    r = draft.draft_spec(case, {"orders": case / "in/orders.csv"},
                         {"picked": case / "out/picked.csv"})
    path = draft.write_spec(r["spec"], case / "spec.json")
    m = runner.run(path, case / "evidence")
    failed = {d["name"] for d in m["check_details"] if d["status"] == "fail"}
    check("picked: order_id unique" in failed, "the duplicated id still fails")
    check("picked: every order_id comes from orders" in failed, "the invented order still fails")
    check("picked: amount not negative" in failed,
          "a negative amount in the output does not switch off the check that catches it")
    check(any("mirror this file" in n for n in r["notes"]),
          "the draft says its structure checks only mirror the output")
    case2 = fresh("refunds")
    write_csv(case2 / "in.csv", ORDERS + [{"order_id": "A-4", "sku": "S-10", "amount": "-30"}],
              ["order_id", "sku", "amount"])
    write_csv(case2 / "out.csv", ORDERS[:1], ["order_id", "sku", "amount"])
    r2 = draft.draft_spec(case2, {"orders": case2 / "in.csv"}, {"out": case2 / "out.csv"})
    check(not by_type(r2["spec"], "numeric_range") and any("already has negative" in n for n in r2["notes"]),
          "when the INPUT has negative amounts (refunds), no bound is proposed, and the note says why")
    check(m["verdict"] != "pass", f"drafting from a wrong output does not make it pass: {m['verdict']}")


def test_documents_and_text() -> None:
    print("\n=== JSON documents, text, missing outputs ===")
    case = fresh("docs")
    (case / "out").mkdir()
    (case / "out" / "summary.json").write_text(json.dumps(
        {"generated_for": "Finance", "items": [{"order_id": "A-1", "amount": 1},
                                               {"order_id": "A-2", "amount": 2}]}), encoding="utf-8")
    (case / "out" / "report.md").write_text("# Review\n\n## Summary\n\ntext\n\n## Items\n", encoding="utf-8")
    r = draft.draft_spec(case, {}, {"summary": case / "out/summary.json",
                                    "report": case / "out/report.md",
                                    "later": case / "out/not_yet.csv"})
    spec = r["spec"]
    keys = by_type(spec, "json_keys")
    check(keys and keys[0]["required"] == ["generated_for", "items"], "top-level keys are required")
    nested = [c for c in spec["checks"] if c.get("records_key") == "items"]
    check(any(c["type"] == "unique_key" and c["key"] == "order_id" for c in nested),
          "an array of objects inside a document is checked as records")
    text = by_type(spec, "text_contains")
    check(text and text[0]["contains"] == ["# Review", "## Summary", "## Items"],
          "headings become required sections")
    check(by_type(spec, "no_placeholders"), "text gets a placeholder check")
    later = [c for c in spec["checks"] if c.get("target") == "outputs.later"]
    check([c["type"] for c in later] == ["file_exists"], "an output not produced yet gets existence only")
    check(any("not produced yet" in n for n in r["notes"]), "and a note says so")


def write_log(path: Path, cycles: int, broken: int = -1) -> None:
    phases = ["pickup", "lift", "travel", "place", "return"]
    lines = ["runner start"]
    t = 0
    for c in range(cycles):
        for p in phases:
            if c == broken and p in ("place", "return"):
                continue
            t += 1
            lines.append("CONT " + json.dumps({"cycle": c, "phase": p, "t": t, "pos": {"y": 0.4}}))
        lines.append("CONT_RESULT " + json.dumps({"cycle": c}))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(("\r\n".join(lines) + "\r\n").encode("utf-16"))


def test_traces() -> None:
    print("\n=== traces ===")
    case = fresh("traces")
    write_log(case / "logs" / "run.log", cycles=6, broken=4)
    r = draft.draft_spec(case, {}, {}, [case / "logs/run.log"])
    spec = r["spec"]
    order = by_type(spec, "trace_order")
    check(order and order[0]["line_prefix"] == "CONT ", "the line prefix is detected")
    check(order and order[0]["group_by"] == "cycle", "the grouping field is found")
    check(order and [s["eq"] for s in order[0]["steps"]] == ["pickup", "lift", "travel", "place", "return"],
          f"the milestones are found in order: {[s['eq'] for s in order[0]['steps']] if order else None}")
    check(order and order[0]["min_groups"] == 6, "min_groups is the strictest value, not the pass count")
    check(not by_type(spec, "trace_always"), "no invariant is fitted to the run")
    path = draft.write_spec(spec, case / "spec.json")
    m = runner.run(path, case / "evidence")
    failed = {d["name"] for d in m["check_details"] if d["status"] == "fail"}
    check("each cycle runs its milestones in order" in failed, "the broken cycle fails the drafted order")
    once = json.dumps(draft.draft_spec(case, {}, {}, [case / "logs/run.log"])["spec"], sort_keys=True)
    check(once == json.dumps(spec, sort_keys=True), "drafting twice gives the same spec")


def test_cli_and_safety() -> None:
    print("\n=== CLI and overwrite safety ===")
    case = fresh("cli")
    write_csv(case / "in.csv", ORDERS, ["order_id", "sku", "amount"])
    cmd = [sys.executable, "-m", "taskattest", "draft", "--input", f"orders={case / 'in.csv'}",
           "--out", str(case / "spec.json")]
    first = subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT))
    check(first.returncode == 0 and (case / "spec.json").is_file(), "taskattest draft writes a spec")
    again = subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT))
    check(again.returncode == 2 and "refusing to overwrite" in again.stdout,
          "drafting again refuses to overwrite a spec someone may have reviewed")
    run = subprocess.run([sys.executable, "-m", "taskattest", "--spec", str(case / "spec.json")],
                         capture_output=True, text=True, cwd=str(ROOT))
    check(run.returncode == 0 and "WARNING unreviewed draft" in run.stdout,
          "the --spec mode still works and prints the draft warning")


def main() -> int:
    test_records()
    test_no_fitting()
    test_documents_and_text()
    test_traces()
    test_cli_and_safety()
    print("\n=== summary ===")
    if failures:
        for f in failures:
            print("FAILED:", f)
        return 1
    print("draft: proposals, no fitting to wrong outputs, documents, traces, CLI and overwrite safety")
    return 0


if __name__ == "__main__":
    sys.exit(main())
