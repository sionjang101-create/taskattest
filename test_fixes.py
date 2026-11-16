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

"""Fix round trip: apply only what the fixes say, run again, and the verdict must be pass.

A small "agent" below acts on the structured fix objects and nothing else. It
does not read the spec or the inputs on its own. If the fixes are sufficient,
its edits turn a partial verdict into a pass. The test also checks the two
rules that keep fixes from gaming a verdict: a broken input seal says to
restore the input, and a process failure says to rerun, not to edit files.
"""

from __future__ import annotations

import csv
import json
import shutil
import sys
import tempfile
from pathlib import Path

import selftest
from taskattest import runner

ROOT = Path(__file__).parent.resolve()
failures: list = []


def check(condition: bool, label: str) -> None:
    print(("OK  " if condition else "NG  ") + label)
    if not condition:
        failures.append(label)


# ---------- a minimal agent that only follows fixes ----------

def _read(path: Path, records_key):
    if path.suffix == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            return list(reader), reader.fieldnames, None
    doc = json.loads(path.read_text(encoding="utf-8"))
    return doc[records_key], None, doc


def _write(path: Path, rows, header, doc, records_key):
    if path.suffix == ".csv":
        with path.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=header, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        return
    doc[records_key] = rows
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def apply_fix(base: Path, fix: dict, originals: dict) -> str:
    action = fix["action"]
    path = base / fix["file"] if isinstance(fix.get("file"), str) else None
    rk = fix.get("records_key")
    if action in ("remove_records", "reconcile", "dedupe"):
        rows, header, doc = _read(path, rk)
        key = fix["key"]
        drop = set(fix.get("values", [])) | set(fix.get("remove", []))
        rows = [r for r in rows if str(r.get(key)) not in drop]
        if action == "dedupe":
            seen, kept = set(), []
            for r in rows:
                if r.get(key) not in seen:
                    seen.add(r.get(key))
                    kept.append(r)
            rows = kept
        for src in fix.get("add_from_input", []):
            rows.append({c: src.get(c, "") for c in (header or src.keys())})
        _write(path, rows, header, doc, rk)
    elif action == "restore_input":
        shutil.copyfile(originals[fix["file"]], path)
    elif action == "add_keys":
        doc = json.loads(path.read_text(encoding="utf-8"))
        for dotted in fix.get("missing_paths", []) + fix.get("empty_paths", []):
            node = doc
            parts = dotted.split(".")
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            if not node.get(parts[-1]):
                node[parts[-1]] = "filled by agent"
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    elif action == "edit_text":
        text = path.read_text(encoding="utf-8")
        for s in fix.get("remove", []):
            text = text.replace(s, "")
        for s in fix.get("add", []):
            text += f"\n{s}\n\n(written by agent)\n"
        path.write_text(text, encoding="utf-8")
    elif action == "rewrite_lines":
        lines = path.read_text(encoding="utf-8").splitlines()
        bad = {h["line"] for h in fix.get("lines", [])}
        path.write_text("\n".join(l for i, l in enumerate(lines, 1) if i not in bad) + "\n",
                        encoding="utf-8")
    return action


def run(spec: Path):
    with tempfile.TemporaryDirectory() as tmp:
        return runner.run(spec, Path(tmp))


def round_trip(label: str, spec: Path, originals: dict) -> None:
    first = run(spec)
    failed = [d for d in first["check_details"] if d["status"] == "fail"]
    check(bool(failed), f"{label}: starts with failures ({len(failed)})")
    check(all(d.get("fix") and d["fix"]["action"] != "review" for d in failed),
          f"{label}: every failure carries a specific fix: "
          f"{[d.get('fix', {}).get('action') for d in failed]}")
    applied = []
    for _ in range(3):  # one fix can make another redundant, so loop until stable
        current = run(spec)
        todo = [d["fix"] for d in current["check_details"] if d["status"] == "fail" and d.get("fix")]
        if not todo:
            break
        for fix in todo:
            if fix["action"] in ("correct_total", "fix_values"):
                continue  # facts only; resolved by the row-level fixes
            applied.append(apply_fix(spec.parent, fix, originals))
    final = run(spec)
    check(final["verdict"] == "pass",
          f"{label}: after applying only the fixes ({sorted(set(applied))}) the verdict is "
          f"{final['verdict']}")


def main() -> int:
    print("=== round trip: the 60-second example ===")
    with tempfile.TemporaryDirectory() as tmp:
        case = Path(tmp) / "agent_says_done"
        shutil.copytree(ROOT / "examples" / "agent_says_done", case)
        round_trip("unpaid orders", case / "spec.json", {})

    print("\n=== round trip: JSON summary + report with seven seeded flaws ===")
    spec = selftest.build_report_case("fix_round_trip", flawed=True)
    original = spec.parent / "original_source.csv"
    selftest.write_csv(original, selftest.ORDERS, ["order_id", "customer", "amount"])
    round_trip("deliverable", spec, {"in/source.csv": original})

    print("\n=== the rules that keep fixes honest ===")
    m = run(spec.parent / "spec.json")
    spec2 = selftest.build_report_case("fix_rules", flawed=True)
    m = run(spec2)
    seal = [d["fix"] for d in m["check_details"] if d["name"] == "input not tampered with"][0]
    check(seal["action"] == "restore_input" and "do not edit inputs" in seal["note"],
          "a broken seal says restore the input, never edit it")
    items = [d["fix"] for d in m["check_details"] if d["name"] == "no invented orders"][0]
    check(items.get("records_key") == "items", "a fix inside a JSON document says which array")
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        (d / "t.jsonl").write_text('{"phase": "a"}\n', encoding="utf-8")
        (d / "spec.json").write_text(json.dumps({"task_id": "t", "inputs": {"t": "t.jsonl"}, "outputs": {},
            "checks": [{"name": "b happened", "type": "trace_eventually", "target": "inputs.t",
                        "condition": {"field": "phase", "eq": "b"}}]}), encoding="utf-8")
        tr = run(d / "spec.json")["check_details"][0]["fix"]
    check(tr["action"] == "rerun_task" and "cannot fix" in tr["note"],
          "a process failure says rerun, not edit the outputs")

    if failures:
        for f in failures:
            print("FAILED:", f)
        return 1
    print("\nfixes: sufficient to reach pass, and honest about inputs and process")
    return 0


if __name__ == "__main__":
    sys.exit(main())
