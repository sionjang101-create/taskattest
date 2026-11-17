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

"""The examples must behave exactly as their READMEs say, on every platform."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from taskattest import runner

ROOT = Path(__file__).parent.resolve()
failures: list = []


def check(condition: bool, label: str) -> None:
    print(("OK  " if condition else "NG  ") + label)
    if not condition:
        failures.append(label)


def main() -> int:
    print("=== examples/agent_says_done ===")
    with tempfile.TemporaryDirectory() as tmp:
        m = runner.run(ROOT / "examples" / "agent_says_done" / "spec.json", Path(tmp))
    status = {d["name"]: d["status"] for d in m["check_details"]}
    check(m["verdict"] == "partial", f"verdict partial, as documented: {m['verdict']}")
    check(status.get("inputs untouched") == "pass",
          "the input seal holds after checkout (line endings preserved by .gitattributes)")
    check({n for n, s in status.items() if s == "fail"} ==
          {"no invented orders", "every unpaid order listed once", "total matches"},
          f"exactly the documented checks fail: {sorted(n for n, s in status.items() if s == 'fail')}")
    readme = (ROOT / "examples" / "agent_says_done" / "README.md").read_text(encoding="utf-8")
    for d in m["check_details"]:
        check(f"{d['status']:7} {d['name']}: {d['detail']}" in readme,
              f"README shows the real output for '{d['name']}'")
    print("\n=== examples/recording_quality ===")
    base = ROOT / "examples" / "recording_quality"
    with tempfile.TemporaryDirectory() as tmp:
        m = runner.run(base / "spec.json", Path(tmp))
    status = {d["name"]: d["status"] for d in m["check_details"]}
    check({n for n, s in status.items() if s == "fail"} ==
          {"every session recorded", "no dropped frames", "clock steady at 20 Hz",
           "arm never frozen for more than 2 s", "every session ends done"} and
          status.get("joints finite and in range") == "pass",
          f"each hidden problem fails its check and the range check passes: {sorted(status.items())}")
    check((base / "recordings" / "session5.jsonl").stat().st_size == 0, "session 5 is the empty recording")
    readme = (base / "README.md").read_text(encoding="utf-8")
    for d in m["check_details"]:
        check(f"{d['status']:7} {d['name']}: {d['detail']}" in readme,
              f"README shows the real output for '{d['name']}'")
    if failures:
        for f in failures:
            print("FAILED:", f)
        return 1
    print("examples: behave as documented")
    return 0


if __name__ == "__main__":
    sys.exit(main())
