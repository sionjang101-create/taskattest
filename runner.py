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

"""TaskProof - did the agent actually finish the job?

Checks the files an AI agent left behind against a declared definition of "done",
using deterministic rules only, and writes a replayable evidence bundle.

Design rules
- Deterministic judging only. No model calls, no network.
- Zero third-party dependencies. Standard library only.
- Same inputs produce the same evidence digest (timestamps and run ids excluded).
- Three verdicts only: pass / partial / fail.

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

TOOL_VERSION = "0.1.0"
CHUNK = 1 << 16


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


def read_csv_rows(path: Path) -> list:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return [dict(row) for row in csv.DictReader(f)]


def to_minor_units(value: str) -> int:
    """Parse an amount into integer minor units, so money never touches a float."""
    s = (value or "").strip().replace(",", "")
    if s == "":
        return 0
    if "." in s:
        whole, frac = s.split(".", 1)
        frac = (frac + "00")[:2]
    else:
        whole, frac = s, "00"
    negative = whole.startswith("-")
    whole = whole.lstrip("+-") or "0"
    total = int(whole) * 100 + int(frac)
    return -total if negative else total


def format_amount(minor: int) -> str:
    sign = "-" if minor < 0 else ""
    minor = abs(minor)
    return f"{sign}{minor // 100}.{minor % 100:02d}"


# ---------- checks ----------

def check_file_exists(ctx, args) -> dict:
    path = ctx["resolve"](args["target"])
    ok = path.is_file()
    return {"status": "pass" if ok else "fail",
            "detail": f"{'exists' if ok else 'missing'}: {ctx['rel'](path)}"}


def check_csv_columns(ctx, args) -> dict:
    path = ctx["resolve"](args["target"])
    if not path.is_file():
        return {"status": "fail", "detail": "file not found"}
    rows = read_csv_rows(path)
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        header = next(csv.reader(f), [])
    missing = [c for c in args["columns"] if c not in header]
    return {"status": "pass" if not missing else "fail",
            "detail": f"{len(rows)} rows, missing columns: {missing or 'none'}"}


def recompute_unpaid(ctx, args):
    """Recompute, from the inputs, which keys should appear in the output.

    The output file is never trusted as the source of truth.
    """
    orders = read_csv_rows(ctx["resolve"](args["orders"]))
    payments = read_csv_rows(ctx["resolve"](args["payments"]))
    key = args.get("key", "order_id")
    amount_column = args.get("amount_column", "amount")
    paid = {r[key] for r in payments if r.get(key)}
    expected = {r[key] for r in orders if r.get(key) and r[key] not in paid}
    amounts = {r[key]: to_minor_units(r.get(amount_column, "0"))
               for r in orders if r.get(key)}
    return expected, amounts


def check_reconcile_rows(ctx, args) -> dict:
    out_path = ctx["resolve"](args["output"])
    if not out_path.is_file():
        return {"status": "fail", "detail": "output file not found"}
    expected, _ = recompute_unpaid(ctx, args)
    key = args.get("key", "order_id")
    actual = {r[key] for r in read_csv_rows(out_path) if r.get(key)}
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    status = "pass" if not missing and not unexpected else "fail"
    return {"status": status,
            "detail": (f"expected {len(expected)} / actual {len(actual)}, "
                       f"missing {missing or 'none'}, unexpected {unexpected or 'none'}"),
            "missing_keys": missing, "unexpected_keys": unexpected}


def check_amount_sum(ctx, args) -> dict:
    out_path = ctx["resolve"](args["output"])
    if not out_path.is_file():
        return {"status": "fail", "detail": "output file not found"}
    expected_keys, amounts = recompute_unpaid(ctx, args)
    column = args.get("amount_column", "amount")
    expected_total = sum(amounts[k] for k in expected_keys if k in amounts)
    actual_total = sum(to_minor_units(r.get(column, "0")) for r in read_csv_rows(out_path))
    return {"status": "pass" if expected_total == actual_total else "fail",
            "detail": (f"expected total {format_amount(expected_total)} / "
                       f"actual total {format_amount(actual_total)}")}


CHECKS = {
    "file_exists": check_file_exists,
    "csv_columns": check_csv_columns,
    "reconcile_rows": check_reconcile_rows,
    "amount_sum": check_amount_sum,
}


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
        fn = CHECKS.get(item["type"])
        if fn is None:
            results.append({"name": item.get("name", item["type"]), "type": item["type"],
                            "status": "fail", "detail": "unknown check type"})
            continue
        out = fn(ctx, item)
        out.update({"name": item.get("name", item["type"]), "type": item["type"]})
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
        "tool": "taskproof",
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
    manifest["replay_command"] = f'python "{Path(__file__).resolve()}" --spec "{spec_path}"'
    manifest["judged_by"] = "deterministic_rules_only"
    manifest["model_calls"] = 0

    out_dir = evidence_root / str(spec.get("task_id", "task")) / digest[:12]
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest["evidence_dir"] = str(out_dir)
    return manifest


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="TaskProof - verify agent task completion from files")
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
