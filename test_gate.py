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

"""Completion gate: drive `taskattest gate` the way a Claude Code Stop hook does.

Hook input arrives as JSON on stdin; the gate answers on stdout. Checked: no spec
lets the agent stop, a failing task blocks with fixes in the reason, the same
failure repeated hands over to a person instead of trapping the agent, and a
fixed task passes.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
failures: list = []


def check(condition: bool, label: str) -> None:
    print(("OK  " if condition else "NG  ") + label)
    if not condition:
        failures.append(label)


def stop_hook(cwd: Path, extra_env=None) -> dict:
    env = dict(__import__("os").environ, **(extra_env or {}))
    out = subprocess.run([sys.executable, "-m", "taskattest", "gate"], cwd=str(ROOT), env=env,
                         input=json.dumps({"hook_event_name": "Stop", "cwd": str(cwd),
                                           "stop_hook_active": False}),
                         capture_output=True, text=True, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout) if out.stdout.strip() else {}


def project(tmp: Path) -> Path:
    case = tmp / "project"
    shutil.copytree(ROOT / "examples" / "agent_says_done", case)
    spec = json.loads((case / "spec.json").read_text(encoding="utf-8"))
    for group in ("inputs", "outputs"):
        for k in spec[group]:
            spec[group][k] = "../" + spec[group][k]
    (case / ".taskattest").mkdir()
    (case / ".taskattest" / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
    return case


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        print("=== no definition of done ===")
        empty = tmp / "empty"
        empty.mkdir()
        check(stop_hook(empty) == {}, "without .taskattest/spec.json the agent stops normally")

        print("\n=== a false claim of done ===")
        case = project(tmp)
        first = stop_hook(case)
        check(first.get("decision") == "block", "a failing task blocks the stop")
        reason = first.get("reason", "")
        check("A-1009" in reason and "A-1005" in reason and "fix (structured)" in reason,
              "the reason names what is wrong and carries the structured fixes")
        check("do not edit the spec or the inputs" in reason, "the reason forbids gaming the spec")

        print("\n=== the same failure again: hand over, do not trap ===")
        second = stop_hook(case)
        check(second.get("decision") == "block", "second identical failure still blocks")
        third = stop_hook(case)
        check("decision" not in third and "allowed to stop for a person" in third.get("systemMessage", ""),
              "third identical failure lets the agent stop and tells the person")
        (tmp / "b").mkdir()
        lower = project(tmp / "b")
        quick = stop_hook(lower, {"TASKATTEST_GATE_REPEATS": "1"})
        check("decision" not in quick, "the hand-over threshold is configurable")

        print("\n=== fixed ===")
        (case / "out" / "unpaid.csv").write_text("order_id,amount\nA-1002,85500\nA-1003,47000\nA-1005,9900\n",
                                                 encoding="utf-8")
        done = stop_hook(case)
        check("decision" not in done and "pass" in done.get("systemMessage", ""), "a fixed task passes")
        state = json.loads((case / ".taskattest" / "gate_state.json").read_text(encoding="utf-8"))
        check(state["last"] == "pass", "the repeat counter resets on pass")
        bundles = list((case / ".taskattest" / "evidence").glob("*/*/manifest.json"))
        check(len(bundles) >= 2, f"every gate run leaves an evidence bundle: {len(bundles)}")

    if failures:
        for f in failures:
            print("FAILED:", f)
        return 1
    print("\ngate: allows without a spec, blocks with fixes, hands over on repeats, passes when fixed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
