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

"""Command check: the task is done only if a command (the tests, a build) succeeds.

Running a command from a spec is a privilege. The completion gate runs as the
person, outside the agent's permission system, so an agent that may edit files
but not run commands could otherwise write a command into the spec and have the
gate run it. Two rules close that:

- a command runs only after a person approved that exact command and working
  directory with `taskattest approve`. Approvals live outside the project, in
  ~/.taskattest/approved_commands.json (or $TASKATTEST_HOME), so a file edit in
  the project cannot grant one;
- the command is an argument list run without a shell, with a time limit.

The verdict records the command and its exit code. The output goes to the
detail and the fix, outside the evidence digest, because test output carries
timings that change from run to run.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_TIMEOUT = 300
MAX_TIMEOUT = 3600
TAIL_LINES = 40
TAIL_CHARS = 4000
APPROVAL_NOTE = ("a person must approve this exact command with `taskattest approve --spec <spec>`; "
                 "an agent must not approve commands it wrote, and must not edit the spec to drop the check")


class CommandError(Exception):
    pass


def approvals_path() -> Path:
    home = os.environ.get("TASKATTEST_HOME")
    return (Path(home) if home else Path.home() / ".taskattest") / "approved_commands.json"


def argv_of(args: dict) -> list:
    cmd = args.get("command")
    if not isinstance(cmd, list) or not cmd or not all(isinstance(c, str) and c for c in cmd):
        raise CommandError('command is required: a list of arguments, such as ["{python}", "-m", "pytest", "-q"]. '
                           "A single string is refused because it would need a shell")
    return cmd


def expand(argv: list) -> list:
    """{python} is the interpreter running taskattest, so a spec stays portable."""
    return [sys.executable if a == "{python}" else a for a in argv]


def fingerprint(argv: list, cwd: Path) -> str:
    """What a person approves: the command as written and the resolved working directory."""
    payload = json.dumps({"command": argv, "cwd": str(cwd)}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_approvals() -> dict:
    try:
        return json.loads(approvals_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def approve(argv: list, cwd: Path) -> str:
    path = approvals_path()
    data = load_approvals()
    fp = fingerprint(argv, cwd)
    data[fp] = {"command": argv, "cwd": str(cwd),
                "approved_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return fp


def _tail(text: str) -> str:
    lines = text.splitlines()[-TAIL_LINES:]
    out = "\n".join(lines)
    return out[-TAIL_CHARS:]


def check_command(cwd: Path, args: dict) -> dict:
    argv = argv_of(args)
    if not cwd.is_dir():
        raise CommandError(f"cwd is not a directory: {cwd}")
    timeout = args.get("timeout", DEFAULT_TIMEOUT)
    if not isinstance(timeout, (int, float)) or not 0 < timeout <= MAX_TIMEOUT:
        raise CommandError(f"timeout must be between 1 and {MAX_TIMEOUT} seconds")
    expect = args.get("expect_exit", 0)
    if not isinstance(expect, int):
        raise CommandError("expect_exit must be an integer")

    fp = fingerprint(argv, cwd)
    result = {"command": argv, "expect_exit": expect, "fingerprint": fp}
    if fp not in load_approvals():
        result.update({"status": "fail", "approved": False, "exit_code": None,
                       "detail": (f"not run: {argv} in {cwd} has not been approved by a person; "
                                  f"the fix carries the exact approve command")})
        return result
    result["approved"] = True
    try:
        proc = subprocess.run(expand(argv), cwd=str(cwd), capture_output=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        raise CommandError(f"no such program on PATH: {argv[0]}")
    except subprocess.TimeoutExpired as exc:
        output = ((exc.stdout or b"") + (exc.stderr or b"")).decode("utf-8", "replace")
        result.update({"status": "fail", "exit_code": None, "timed_out": True, "output_tail": _tail(output),
                       "detail": f"timed out after {timeout}s: {' '.join(argv)}"})
        return result
    output = (proc.stdout + proc.stderr).decode("utf-8", "replace")
    ok = proc.returncode == expect
    result.update({"status": "pass" if ok else "fail", "exit_code": proc.returncode, "timed_out": False,
                   "output_tail": _tail(output)})
    last = next((l for l in reversed(output.splitlines()) if l.strip()), "")
    result["detail"] = (f"exit {proc.returncode}{' as expected' if ok else f', expected {expect}'}: "
                        f"{' '.join(argv)}" + (f" | {last.strip()[:200]}" if last else ""))
    return result


COMMAND_DOCS = {
    "command_succeeds": {
        "family": "command",
        "verifies": ("a command exits with the expected code, such as the test suite passing. Runs without "
                     "a shell, with a time limit, and only after a person approved that exact command and "
                     "working directory with `taskattest approve --spec <spec>`; an unapproved command is "
                     "not run and the check fails. {python} in the command is replaced by the interpreter "
                     "running taskattest. The exit code is evidence; the output is shown but not digested"),
        "fields": {"command": "argument list, such as [\"{python}\", \"-m\", \"pytest\", \"-q\"]",
                   "cwd": "working directory relative to the spec, default the spec's directory "
                          "(\"..\" for a spec in .taskattest/)",
                   "expect_exit": "expected exit code, default 0",
                   "timeout": f"seconds, default {DEFAULT_TIMEOUT}, at most {MAX_TIMEOUT}"},
    },
}


def approve_main(argv) -> int:
    """`taskattest approve --spec <spec> [--yes]`: a person approves the spec's commands."""
    import argparse
    from . import runner
    ap = argparse.ArgumentParser(prog="taskattest approve",
                                 description="Approve the commands a spec runs. Only a person should do this.")
    ap.add_argument("--spec", required=True)
    ap.add_argument("--yes", action="store_true", help="approve without asking (you have read them)")
    a = ap.parse_args(argv)
    spec_path = Path(a.spec).resolve()
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    items = [c for c in spec.get("checks", []) if c.get("type") == "command_succeeds"]
    if not items:
        print("this spec has no command_succeeds checks")
        return 0
    approved = load_approvals()
    pending = []
    for item in items:
        cmd = argv_of(item)
        cwd = runner.resolve_ref(spec_path.parent.resolve(), spec, item.get("cwd", "."))
        state = "approved" if fingerprint(cmd, cwd) in approved else "NOT approved"
        print(f"- {item.get('name', 'command')}: {cmd}\n    in {cwd}  [{state}]")
        if state != "approved":
            pending.append((cmd, cwd))
    if not pending:
        return 0
    if not a.yes:
        if not sys.stdin.isatty():
            print("not a terminal: rerun with --yes after reading the commands above", file=sys.stderr)
            return 1
        try:
            answer = input(f"Run these {len(pending)} command(s) whenever this spec is checked? [y/N] ")
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer.strip().lower() != "y":
            print("\nnothing approved")
            return 1
    for cmd, cwd in pending:
        approve(cmd, cwd)
    print(f"approved {len(pending)} command(s) in {approvals_path()}")
    return 0
