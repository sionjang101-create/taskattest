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

"""Completion gate: a coding agent's "done" is checked against the spec before it can stop.

Run as a Claude Code Stop hook (and any agent whose stop hook passes JSON on stdin):

    taskattest gate

It looks for `.taskattest/spec.json` in the session's working directory.
- No spec: the agent stops normally. A gate that blocks work it was never told
  about would get turned off, so absence of a definition of done is not a failure.
- Pass: the agent stops normally.
- Fail: the stop is blocked, and the reason lists each failure with its fix, so
  the agent keeps working on exactly what is wrong.
- The same failure again and again: the gate stops blocking and hands over to a
  person. An agent that cannot fix something should not be trapped by the gate;
  the repeated attempts are themselves the evidence a human needs.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

from . import fixes, runner

SPEC_DIR = ".taskattest"
STATE_FILE = "gate_state.json"
DEFAULT_REPEATS = 3


def _signature(manifest: dict) -> str:
    failed = [(d["name"], fixes.summary(d.get("fix"))) for d in manifest["check_details"]
              if d["status"] == "fail"]
    return hashlib.sha256(json.dumps(failed, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def _reason(manifest: dict) -> str:
    lines = [f"taskattest gate: the task is not done ({manifest['verdict']}). "
             f"Fix these, then finish again:"]
    for d in manifest["check_details"]:
        if d["status"] != "fail":
            continue
        line = f"- {d['name']}: {d['detail']}"
        fix = d.get("fix")
        if fix:
            line += f"\n  fix: {fixes.summary(fix)}"
            line += f"\n  fix (structured): {json.dumps(fix, ensure_ascii=False)}"
        lines.append(line)
    lines.append("The spec is the definition of done; do not edit the spec or the inputs to pass.")
    return "\n".join(lines)


def evaluate(cwd: Path, max_repeats: int = DEFAULT_REPEATS) -> dict:
    """Decide for a stop attempt. Returns the hook output to print, or {} to allow."""
    base = Path(cwd) / SPEC_DIR
    spec = Path(os.environ.get("TASKATTEST_SPEC") or base / "spec.json")
    if not spec.is_file():
        return {}
    base.mkdir(parents=True, exist_ok=True)
    manifest = runner.run(spec, base / "evidence")
    state_path = base / STATE_FILE
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = {}

    if manifest["verdict"] == "pass":
        state_path.write_text(json.dumps({"last": "pass", "digest": manifest["evidence_digest"]}),
                              encoding="utf-8")
        return {"systemMessage": f"taskattest gate: pass ({manifest['evidence_digest'][:12]})"}

    sig = _signature(manifest)
    repeats = state.get("repeats", 0) + 1 if state.get("signature") == sig else 1
    state_path.write_text(json.dumps({"last": manifest["verdict"], "signature": sig, "repeats": repeats,
                                      "digest": manifest["evidence_digest"]}), encoding="utf-8")
    if repeats >= max_repeats:
        return {"systemMessage": (f"taskattest gate: the same failure came back {repeats} times, so the "
                                  f"agent is being allowed to stop for a person to review. "
                                  f"Evidence: {manifest['evidence_dir']}\n" + _reason(manifest))}
    return {"decision": "block", "reason": _reason(manifest)}


def main(argv=None) -> int:
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    try:
        event = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        event = {}
    cwd = Path(event.get("cwd") or os.getcwd())
    try:
        repeats = int(os.environ.get("TASKATTEST_GATE_REPEATS", DEFAULT_REPEATS))
    except ValueError:
        repeats = DEFAULT_REPEATS
    try:
        out = evaluate(cwd, repeats)
    except Exception as exc:  # a broken gate must never trap the agent
        out = {"systemMessage": f"taskattest gate could not run and did not block: {type(exc).__name__}: {exc}"}
    if out:
        sys.stdout.reconfigure(encoding="utf-8") if hasattr(sys.stdout, "reconfigure") else None
        print(json.dumps(out, ensure_ascii=False))
    return 0
