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

"""Command check: approved commands run and decide; unapproved ones never run.

The approval store is pointed at a temporary TASKATTEST_HOME, so nothing here
touches the real one.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(ROOT))

failures: list = []


def check(condition: bool, label: str) -> None:
    print(("OK  " if condition else "NG  ") + label)
    if not condition:
        failures.append(label)


def main() -> int:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        tmp = Path(tmp)
        os.environ["TASKATTEST_HOME"] = str(tmp / "home")
        from taskattest import commands, gate, runner
        from taskattest.mcp_server import ToolError, guard_spec_refs

        project = tmp / "project"
        (project / ".taskattest").mkdir(parents=True)
        marker = project / "ran.txt"
        touch = ["{python}", "-c", "import pathlib; pathlib.Path('ran.txt').write_text('x')"]

        def spec(items: list) -> Path:
            p = project / ".taskattest" / "spec.json"
            checks = [dict({"type": "command_succeeds", "name": f"cmd{i}", "cwd": ".."}, **it)
                      for i, it in enumerate(items)]
            p.write_text(json.dumps({"task_id": "cmd-demo", "checks": checks}), encoding="utf-8")
            return p

        def run(items: list) -> dict:
            return runner.run(spec(items), project / ".taskattest" / "evidence")

        def approve(items: list) -> int:
            return commands.approve_main(["--spec", str(spec(items)), "--yes"])

        print("=== nothing runs without a person's approval ===")
        m = run([{"command": touch}])
        d = m["check_details"][0]
        check(m["verdict"] == "fail" and not marker.exists(), "an unapproved command is not run and the check fails")
        check(d["fix"]["action"] == "approve_command" and "must not approve" in d["fix"]["note"],
              "the fix asks for a person's approval and forbids self-approval")
        line = d["fix"]["approve_with"]
        check("approve --spec" in line and str((project / ".taskattest" / "spec.json").resolve()) in line,
              f"the fix carries the exact approve command for this install: {line[:90]}")
        (project / ".taskattest" / "approved_commands.json").write_text(
            json.dumps({commands.fingerprint(touch, project.resolve()): {}}), encoding="utf-8")
        check(run([{"command": touch}])["verdict"] == "fail" and not marker.exists(),
              "an approvals file written inside the project grants nothing")
        check(project.resolve() not in commands.approvals_path().resolve().parents,
              "approvals are stored outside the project")

        print("\n=== approved commands decide the check ===")
        check(approve([{"command": touch}]) == 0, "taskattest approve --yes records the approval")
        m = run([{"command": touch}])
        check(m["verdict"] == "pass" and marker.exists(), "an approved command runs, in cwd, and its exit decides")
        fail_cmd = ["{python}", "-c", "import sys; print('3 tests failed'); sys.exit(3)"]
        approve([{"command": fail_cmd}])
        m = run([{"command": fail_cmd}])
        d = m["check_details"][0]
        check(m["verdict"] == "fail" and m["checks"][0]["exit_code"] == 3, "a non-zero exit fails")
        check(d["fix"]["action"] == "make_command_pass" and "3 tests failed" in d["fix"]["output_tail"]
              and "do not edit the command" in d["fix"]["note"],
              "the fix carries the output tail and forbids editing the command or the tests' expectations")
        check(run([{"command": fail_cmd, "expect_exit": 3}])["verdict"] == "pass",
              "expect_exit changes the code that counts as success")

        print("\n=== approval is for the exact command and directory ===")
        changed = touch + ["extra"]
        check(run([{"command": changed}])["checks"][0]["approved"] is False,
              "a changed command needs a new approval")
        (project / "sub").mkdir()
        check(run([{"command": touch, "cwd": "../sub"}])["checks"][0]["approved"] is False,
              "the same command in another directory needs a new approval")

        print("\n=== evidence, limits and errors ===")
        noisy = ["{python}", "-c", "import time; print(time.time())"]
        approve([{"command": noisy}])
        a, b = run([{"command": noisy}]), run([{"command": noisy}])
        check(a["evidence_digest"] == b["evidence_digest"] and a["check_details"][0]["output_tail"]
              != b["check_details"][0]["output_tail"],
              "output that changes between runs does not change the evidence digest")
        slow = ["{python}", "-c", "import time; time.sleep(5)"]
        approve([{"command": slow, "timeout": 1}])
        m = run([{"command": slow, "timeout": 1}])
        check(m["verdict"] == "fail" and m["checks"][0].get("timed_out") is True, "a command over its time limit fails")
        m = run([{"command": "pytest -q"}])
        check(m["check_details"][0]["fix"]["action"] == "fix_spec", "a single command string is refused as a spec error")
        missing = ["no-such-program-xyz"]
        approve([{"command": missing}])
        m = run([{"command": missing}])
        check(m["check_details"][0]["fix"]["action"] == "fix_spec" and "no such program" in m["check_details"][0]["detail"],
              "a program that does not exist is a spec error")
        check(commands.expand(["{python}", "-V"])[0] == sys.executable, "{python} is the interpreter running taskattest")

        print("\n=== approval needs a person ===")
        spec([{"command": ["{python}", "-c", "print(1)"]}])
        p = subprocess.run([sys.executable, "-m", "taskattest", "approve", "--spec",
                            str(project / ".taskattest" / "spec.json")], cwd=str(ROOT), input="y\n",
                           capture_output=True, text=True, env=dict(os.environ))
        check(p.returncode != 0 and "--yes" in p.stderr, "without a terminal, approve refuses unless --yes is given")

        print("\n=== MCP guard and the completion gate ===")
        outside = spec([{"command": touch, "cwd": "../.."}])
        try:
            guard_spec_refs(outside, project.resolve())
            refused = False
        except ToolError:
            refused = True
        check(refused, "a command directory outside the permitted --root is refused")
        gate_cmd = ["{python}", "-c", "import pathlib, sys; sys.exit(0 if pathlib.Path('ok.txt').exists() else 1)"]
        spec([{"command": gate_cmd}])
        blocked = gate.evaluate(project)
        check(blocked.get("decision") == "block" and "approve" in blocked.get("reason", ""),
              "the gate blocks an unapproved command and says a person must approve it")
        approve([{"command": gate_cmd}])
        (project / ".taskattest" / "gate_state.json").unlink()
        check(gate.evaluate(project).get("decision") == "block", "approved but failing: the gate still blocks")
        (project / "ok.txt").write_text("ok", encoding="utf-8")
        done = gate.evaluate(project)
        check("decision" not in done and "pass" in done.get("systemMessage", ""), "when the command passes the agent may stop")

    if failures:
        for f in failures:
            print("FAILED:", f)
        return 1
    print("\ncommands: only approved commands run; exit codes decide; output stays out of the digest")
    return 0


if __name__ == "__main__":
    sys.exit(main())
