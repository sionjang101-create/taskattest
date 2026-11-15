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

"""Self-test: three fixture cases, three verdicts, and a stable evidence digest.

case_ok              agent produced the correct unpaid-order list   -> pass
case_missing_output  agent produced no output file                  -> fail
case_incomplete      output exists but one row and the total differ -> partial

Each case runs twice; the evidence digest must match across runs.
No network and no model calls are involved.
"""

from __future__ import annotations

import csv
import json
import shutil
import sys
from pathlib import Path

import runner

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


def write_csv(path: Path, rows: list, fields: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def build_case(name: str, produce: str) -> Path:
    case = WORK / name
    if case.exists():
        shutil.rmtree(case)
    (case / "in").mkdir(parents=True)
    write_csv(case / "in" / "orders.csv", ORDERS, ["order_id", "customer", "amount"])
    write_csv(case / "in" / "payments.csv", PAYMENTS, ["order_id", "paid_amount"])
    (case / "spec.json").write_text(json.dumps(SPEC, ensure_ascii=False, indent=2) + "\n",
                                    encoding="utf-8")

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


def main() -> int:
    cases = [("case_ok", "correct", "pass"),
             ("case_missing_output", "none", "fail"),
             ("case_incomplete", "incomplete", "partial")]
    failures = []
    digests = {}

    print("=== verdicts ===")
    for name, produce, expected in cases:
        spec = build_case(name, produce)
        first = runner.run(spec, spec.parent / "evidence")
        second = runner.run(spec, spec.parent / "evidence")
        digests[name] = (first["evidence_digest"], second["evidence_digest"])
        got = first["verdict"]
        if got != expected:
            failures.append(f"{name}: expected {expected}, got {got}")
        if first["model_calls"] != 0 or first["judged_by"] != "deterministic_rules_only":
            failures.append(f"{name}: deterministic judging contract violated")
        print(f"{'OK ' if got == expected else 'NG '}{name}: {got} (expected {expected})")
        print("     " + "; ".join(d["detail"] for d in first["check_details"]))

    print("=== digest stability ===")
    for name, (d1, d2) in digests.items():
        same = d1 == d2
        if not same:
            failures.append(f"{name}: evidence digest differs across runs")
        print(f"{'OK ' if same else 'NG '}{name}: {d1[:12]} / {d2[:12]}")

    print("=== summary ===")
    if failures:
        for f in failures:
            print("FAILED:", f)
        return 1
    print("verdicts 3/3, digest stability 3/3, model calls 0, dependencies 0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
