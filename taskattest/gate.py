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

Run `taskattest gate subagent` as a SubagentStop hook to judge a subagent when
it finishes. A subagent usually does part of the task, so it is held only to the
scope checks: an out-of-scope edit is blocked before it reports back, and the
rest of the definition of done waits for the main agent's stop.

Run `taskattest gate start` as a SessionStart hook as well when the spec has a
scope_changes check. It records the uncommitted work already present, so the
person's own unfinished edits are not blamed on the agent. A resumed or
compacted session keeps the baseline it started with.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import fixes, runner

SPEC_DIR = ".taskattest"
STATE_FILE = "gate_state.json"
SUBAGENT_STATE_FILE = "gate_state_subagent.json"
# A subagent usually does part of the task, so the whole definition of done would
# block it for work that is not its job. It is judged only on what it could have
# broken on its way: the scope.
SUBAGENT_CHECKS = ("scope_changes",)
BASELINE_DIR = "baselines"
DEFAULT_REPEATS = 3


def _signature(manifest: dict) -> str:
    failed = [(d["name"], fixes.summary(d.get("fix"))) for d in manifest["check_details"]
              if d["status"] == "fail"]
    return hashlib.sha256(json.dumps(failed, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def _reason(manifest: dict, subagent: bool = False) -> str:
    if subagent:
        lines = ["taskattest gate: this subagent changed files outside the task's scope. "
                 "Undo these before you report back:"]
    else:
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


def _spec(cwd: Path) -> Path:
    return Path(os.environ.get("TASKATTEST_SPEC") or Path(cwd) / SPEC_DIR / "spec.json")


def _baseline_path(cwd: Path, session_id) -> Path:
    name = "".join(c for c in str(session_id or "default") if c.isalnum() or c in "-_")[:80] or "default"
    return Path(cwd) / SPEC_DIR / BASELINE_DIR / f"{name}.json"


def start(cwd: Path, session_id=None, source: str = "startup") -> dict:
    """SessionStart: record the uncommitted work that is not the agent's. Never blocks."""
    spec = _spec(cwd)
    if not spec.is_file():
        return {}
    path = _baseline_path(cwd, session_id)
    if source in ("resume", "compact") and path.is_file():
        return {}  # the task is still the one that started earlier
    roots = runner.scope_baseline(spec)
    if not roots:
        return {}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"session_id": session_id, "source": source,
                                "taken_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                "roots": roots}, ensure_ascii=False, indent=2), encoding="utf-8")
    kept = sum(len(r["files"]) for r in roots.values())
    return {"systemMessage": f"taskattest gate: scope baseline recorded ({kept} uncommitted path(s) already present)"}


def evaluate(cwd: Path, max_repeats: int = DEFAULT_REPEATS, session_id=None, subagent: bool = False) -> dict:
    """Decide for a stop attempt. Returns the hook output to print, or {} to allow.

    subagent=True is the SubagentStop hook: only scope checks run, with their own repeat counter."""
    base = Path(cwd) / SPEC_DIR
    spec = _spec(cwd)
    if not spec.is_file():
        return {}
    only = None
    if subagent:
        try:
            types = {c.get("type") for c in json.loads(spec.read_text(encoding="utf-8")).get("checks", [])}
        except (OSError, json.JSONDecodeError):
            return {}  # the main Stop gate reports a broken spec; a subagent is not blocked for it
        if not types & set(SUBAGENT_CHECKS):
            return {}
        only = SUBAGENT_CHECKS
    base.mkdir(parents=True, exist_ok=True)
    baseline = None
    bpath = _baseline_path(cwd, session_id)
    if bpath.is_file():
        baseline = json.loads(bpath.read_text(encoding="utf-8")).get("roots")
    manifest = runner.run(spec, base / "evidence", baseline, only)
    state_path = base / (SUBAGENT_STATE_FILE if subagent else STATE_FILE)
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = {}

    if manifest["verdict"] == "pass":
        state_path.write_text(json.dumps({"last": "pass", "digest": manifest["evidence_digest"]}),
                              encoding="utf-8")
        if subagent:
            return {}  # nothing to say when a subagent stayed in scope
        return {"systemMessage": f"taskattest gate: pass ({manifest['evidence_digest'][:12]})"}

    sig = _signature(manifest)
    repeats = state.get("repeats", 0) + 1 if state.get("signature") == sig else 1
    state_path.write_text(json.dumps({"last": manifest["verdict"], "signature": sig, "repeats": repeats,
                                      "digest": manifest["evidence_digest"]}), encoding="utf-8")
    if repeats >= max_repeats:
        return {"systemMessage": (f"taskattest gate: the same failure came back {repeats} times, so the "
                                  f"agent is being allowed to stop for a person to review. "
                                  f"Evidence: {manifest['evidence_dir']}\n" + _reason(manifest, subagent))}
    return {"decision": "block", "reason": _reason(manifest, subagent)}


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    try:
        event = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        event = {}
    cwd = Path(event.get("cwd") or os.getcwd())
    session_id = event.get("session_id")
    try:
        repeats = int(os.environ.get("TASKATTEST_GATE_REPEATS", DEFAULT_REPEATS))
    except ValueError:
        repeats = DEFAULT_REPEATS
    try:
        if argv and argv[0] == "start":
            out = start(cwd, session_id, str(event.get("source") or "startup"))
        elif argv and argv[0] == "subagent":
            out = evaluate(cwd, repeats, session_id, subagent=True)
        else:
            out = evaluate(cwd, repeats, session_id)
    except Exception as exc:  # a broken gate must never trap the agent
        out = {"systemMessage": f"taskattest gate could not run and did not block: {type(exc).__name__}: {exc}"}
    if out:
        sys.stdout.reconfigure(encoding="utf-8") if hasattr(sys.stdout, "reconfigure") else None
        print(json.dumps(out, ensure_ascii=False))
    return 0
