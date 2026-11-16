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

"""Self-test: five fixture cases over two task families, with a stable evidence digest.

Reconciliation (CSV):
  case_ok              agent produced the correct unpaid-order list   -> pass
  case_missing_output  agent produced no output file                  -> fail
  case_incomplete      output exists but one row and the total differ -> partial

Deliverable (JSON summary and markdown report):
  case_report_ok       correct summary, complete report               -> pass
  case_report_bad      invented row, duplicate, out-of-range amount,
                       empty field, placeholder text, tampered input  -> partial

Each case runs twice; the evidence digest must match across runs.
No network and no model calls are involved.
"""

from __future__ import annotations

import csv
import json
import shutil
import sys
from pathlib import Path

from taskattest import runner

ROOT = Path(__file__).parent.resolve()
WORK = ROOT / "fixtures"

ORDERS = [
    {"order_id": "A-1001", "customer": "Northwind Foods", "amount": "120000"},
    {"order_id": "A-1002", "customer": "Harbor Supply", "amount": "85500"},
    {"order_id": "A-1003", "customer": "Daon Trading", "amount": "47000"},
    {"order_id": "A-1004", "customer": "Riverside Mart", "amount": "310000"},
    {"order_id": "A-1005", "customer": "Blue Spring Co", "amount": "9900"},
]
# Paid: 1001 and 1004. So the unpaid set is 1002, 1003, 1005 (total 142400).
PAYMENTS = [
    {"order_id": "A-1001", "paid_amount": "120000"},
    {"order_id": "A-1004", "paid_amount": "310000"},
]

SPEC = {
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

REPORT_CHECKS = [
    {"name": "summary exists", "type": "file_exists", "target": "outputs.summary"},
    {"name": "summary parses", "type": "json_valid", "target": "outputs.summary",
     "top_level": "object"},
    {"name": "summary fields", "type": "json_keys", "target": "outputs.summary",
     "required": ["generated_for", "totals.count", "items.*.order_id"],
     "non_empty": ["generated_for", "items"]},
    {"name": "item count", "type": "row_count", "target": "outputs.summary",
     "records_key": "items", "min": 3},
    {"name": "no duplicate items", "type": "unique_key", "target": "outputs.summary",
     "records_key": "items", "key": "order_id"},
    {"name": "no invented orders", "type": "subset_of", "target": "outputs.summary",
     "records_key": "items", "key": "order_id",
     "source": "inputs.source", "source_key": "order_id"},
    {"name": "amounts in range", "type": "numeric_range", "target": "outputs.summary",
     "records_key": "items", "column": "amount", "aggregate": "each",
     "min": 0, "max": 1000000},
    {"name": "report sections", "type": "text_contains", "target": "outputs.report",
     "contains": ["## Summary", "## Items"], "absent": ["ERROR"]},
    {"name": "nothing left unwritten", "type": "no_placeholders", "target": "outputs.report"},
    {"name": "input not tampered with", "type": "file_hash", "target": "inputs.source",
     "sha256": "filled in when the fixture is built"},
]

GOOD_REPORT = """# Order review

## Summary

Three unpaid orders remain, totalling 142400.

## Items

| order_id | amount |
| --- | --- |
| A-1002 | 85500 |
| A-1003 | 47000 |
| A-1005 | 9900 |
"""

BAD_REPORT = """# Order review

## Summary

TODO: write this section once the numbers are confirmed.
"""

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


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fresh_case(name: str) -> Path:
    case = WORK / name
    if case.exists():
        shutil.rmtree(case)
    case.mkdir(parents=True)
    return case


# ---------- reconciliation fixtures ----------

def build_case(name: str, produce: str) -> Path:
    case = fresh_case(name)
    write_csv(case / "in" / "orders.csv", ORDERS, ["order_id", "customer", "amount"])
    write_csv(case / "in" / "payments.csv", PAYMENTS, ["order_id", "paid_amount"])
    write_json(case / "spec.json", SPEC)

    paid = {p["order_id"] for p in PAYMENTS}
    unpaid = [{"order_id": o["order_id"], "amount": o["amount"]}
              for o in ORDERS if o["order_id"] not in paid]
    if produce == "correct":
        write_csv(case / "out" / "missing_orders.csv", unpaid, ["order_id", "amount"])
    elif produce == "none":
        pass  # the agent never produced the output
    elif produce == "incomplete":
        write_csv(case / "out" / "missing_orders.csv", unpaid[:-1], ["order_id", "amount"])
    return case / "spec.json"


# ---------- deliverable fixtures ----------

def build_report_case(name: str, flawed: bool) -> Path:
    case = fresh_case(name)
    source = case / "in" / "source.csv"
    write_csv(source, ORDERS, ["order_id", "customer", "amount"])

    # Seal the input: the spec records the hash of the file as handed to the agent.
    checks = [dict(c) for c in REPORT_CHECKS]
    checks[-1]["sha256"] = runner.sha256_file(source)
    write_json(case / "spec.json", {
        "task_id": "order_deliverable",
        "description": "Summarise the unpaid orders as JSON and write a short report.",
        "inputs": {"source": "in/source.csv"},
        "outputs": {"summary": "out/summary.json", "report": "out/report.md"},
        "core_output": "summary",
        "checks": checks,
    })

    unpaid = [o for o in ORDERS if o["order_id"] in ("A-1002", "A-1003", "A-1005")]
    items = [{"order_id": o["order_id"], "amount": int(o["amount"])} for o in unpaid]

    if not flawed:
        write_json(case / "out" / "summary.json",
                   {"generated_for": "Finance", "totals": {"count": len(items)},
                    "items": items})
        (case / "out" / "report.md").write_text(GOOD_REPORT, encoding="utf-8")
        return case / "spec.json"

    # Each flaw below targets one check, so a failure names its own cause.
    bad_items = items + [
        {"order_id": "A-9999", "amount": 50000},      # invented      -> subset_of
        {"order_id": "A-1002", "amount": 99999999},   # duplicate     -> unique_key
    ]                                                 # and amount    -> numeric_range
    write_json(case / "out" / "summary.json",
               {"generated_for": "", "totals": {"count": len(bad_items)},
                "items": bad_items})                  # empty field   -> json_keys
    (case / "out" / "report.md").write_text(BAD_REPORT, encoding="utf-8")
    # missing section -> text_contains, TODO -> no_placeholders
    write_csv(source, [dict(o, amount="1") for o in ORDERS],
              ["order_id", "customer", "amount"])      # tampered      -> file_hash
    return case / "spec.json"


# ---------- test body ----------

def main() -> int:
    cases = [("case_ok", lambda n: build_case(n, "correct"), "pass"),
             ("case_missing_output", lambda n: build_case(n, "none"), "fail"),
             ("case_incomplete", lambda n: build_case(n, "incomplete"), "partial"),
             ("case_report_ok", lambda n: build_report_case(n, False), "pass"),
             ("case_report_bad", lambda n: build_report_case(n, True), "partial")]
    digests = {}
    manifests = {}

    print("=== verdicts ===")
    for name, build, expected in cases:
        spec = build(name)
        first = runner.run(spec, spec.parent / "evidence")
        second = runner.run(spec, spec.parent / "evidence")
        digests[name] = (first["evidence_digest"], second["evidence_digest"])
        manifests[name] = first
        check(first["verdict"] == expected,
              f"{name}: {first['verdict']} (expected {expected})")
        check(first["model_calls"] == 0 and first["judged_by"] == "deterministic_rules_only",
              f"{name}: judged by rules only, no model calls")
        for detail in first["check_details"]:
            print(f"       {detail['status']:4} {detail['name']}: {detail['detail']}")

    print("\n=== digest stability ===")
    for name, (one, two) in digests.items():
        check(one == two, f"{name}: {one[:12]} / {two[:12]}")

    print("\n=== every seeded flaw is caught by its own check ===")
    failed = {d["name"] for d in manifests["case_report_bad"]["check_details"]
              if d["status"] == "fail"}
    expected_failures = {"summary fields", "no duplicate items", "no invented orders",
                         "amounts in range", "report sections", "nothing left unwritten",
                         "input not tampered with"}
    check(failed == expected_failures,
          f"exactly the seven seeded flaws failed: {sorted(failed)}")
    clean = [d for d in manifests["case_report_ok"]["check_details"] if d["status"] == "pass"]
    check(len(clean) == len(REPORT_CHECKS),
          f"the clean deliverable passes all {len(REPORT_CHECKS)} checks: {len(clean)}")

    print("\n=== check documentation is in sync ===")
    undocumented = sorted(set(runner.CHECKS) - set(runner.CHECK_DOCS))
    orphaned = sorted(set(runner.CHECK_DOCS) - set(runner.CHECKS))
    check(not undocumented, f"every check type is documented: missing {undocumented or 'none'}")
    check(not orphaned, f"nothing documented that does not exist: {orphaned or 'none'}")

    print("\n=== summary ===")
    if failures:
        for f in failures:
            print("FAILED:", f)
        return 1
    print(f"5 cases, verdicts and digests as expected, {len(runner.CHECKS)} check types, "
          f"model calls 0, dependencies 0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
