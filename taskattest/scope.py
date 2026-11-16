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

"""Scope check: did the agent change only what it was asked to change?

An agent can deliver a correct output and still edit a config, a lockfile, a
test it found inconvenient or a file in another module. Those changes are
invisible to checks that look at the deliverable. This check reads the
working tree of a git repository and compares every changed path against the
declared scope.

What counts as changed, relative to `base` (default HEAD): modified, added,
deleted and renamed tracked files, staged or not, plus untracked files that
git does not ignore. Paths under `.taskattest/` are never counted, because
the gate writes its own evidence there.

Work that was already uncommitted when the task started is not the agent's.
A baseline taken at the start (see `snapshot`, and `taskattest gate start`)
records each such path with its content hash; a path still in exactly that
state is left out. A path the agent changed further is judged as usual, and a
pre-existing change the agent undid is reported too, because discarding a
person's uncommitted work is itself an out-of-scope change.

Globs are matched against paths relative to `root`, with `/` as separator:
`*` stays within one directory, `**` crosses directories, `?` is one
character. `src/**` matches every file under src.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

ALWAYS_IGNORED = ("**/.taskattest/**",)


class ScopeError(Exception):
    pass


def glob_regex(pattern: str) -> re.Pattern:
    """Translate a path glob to a regex. `**/` may match zero directories."""
    p = pattern.strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    out, i = [], 0
    while i < len(p):
        c = p[i]
        if p.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif p.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("".join(out) + r"\Z")


def _matches(path: str, patterns) -> str | None:
    for pat in patterns:
        if glob_regex(pat).match(path):
            return pat
    return None


def _git(cwd: Path, *args: str) -> bytes:
    try:
        out = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, timeout=60)
    except FileNotFoundError:
        raise ScopeError("git is not installed or not on PATH")
    except subprocess.TimeoutExpired:
        raise ScopeError(f"git {args[0]} timed out")
    if out.returncode != 0:
        msg = out.stderr.decode("utf-8", "replace").strip().splitlines()
        raise ScopeError(f"git {args[0]} failed: {msg[-1] if msg else out.returncode}")
    return out.stdout


def changed_paths(root: Path, base: str = "HEAD") -> list:
    """[(status, path relative to root)] for every change in the working tree against base."""
    if not root.is_dir():
        raise ScopeError(f"root is not a directory: {root}")
    top = Path(_git(root, "rev-parse", "--show-toplevel").decode("utf-8").strip()).resolve()
    _git(root, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}")
    prefix = root.resolve().relative_to(top).as_posix() if root.resolve() != top else ""

    def under_root(path: str):
        if not prefix:
            return path
        return path[len(prefix) + 1:] if path.startswith(prefix + "/") else None

    changes = {}
    raw = _git(top, "-c", "core.quotepath=off", "diff", "--name-status", "--no-renames", "-z", base, "--")
    parts = raw.decode("utf-8", "surrogateescape").split("\0")
    for status, path in zip(parts[0::2], parts[1::2]):
        if status and path:
            changes[path] = {"A": "added", "D": "deleted"}.get(status[0], "modified")
    raw = _git(top, "-c", "core.quotepath=off", "ls-files", "--others", "--exclude-standard", "-z")
    for path in raw.decode("utf-8", "surrogateescape").split("\0"):
        if path:
            changes.setdefault(path, "untracked")
    out = []
    for path, status in changes.items():
        rel = under_root(path)
        if rel is not None and not _matches(rel, ALWAYS_IGNORED):
            out.append((status, rel))
    return sorted(out, key=lambda x: x[1])


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def snapshot(root: Path, args: dict) -> dict:
    """The working-tree state before the task: {path: {status, sha256}} relative to root."""
    base = str(args.get("base", "HEAD"))
    return {"base": base, "files": {p: {"status": s, "sha256": _sha256(root / p)}
                                    for s, p in changed_paths(root, base)}}


def check_scope(root: Path, args: dict, baseline: dict | None = None) -> dict:
    allow = args.get("allow")
    if isinstance(allow, str):
        allow = [allow]
    if not allow or not all(isinstance(a, str) and a.strip() for a in allow):
        raise ScopeError("allow is required: a list of path globs the task may change, such as [\"src/**\"]")
    deny = args.get("deny") or []
    if isinstance(deny, str):
        deny = [deny]
    ignore = args.get("ignore") or []
    if isinstance(ignore, str):
        ignore = [ignore]
    base = str(args.get("base", "HEAD"))

    if baseline is not None and baseline.get("base", base) != base:
        raise ScopeError(f"the baseline was taken against {baseline.get('base')}, the check uses {base}")
    before = (baseline or {}).get("files", {})
    changes = [(s, p) for s, p in changed_paths(root, base) if not _matches(p, ignore)]
    current = {}
    for status, path in changes:
        current[path] = {"path": path, "status": status, "sha256": _sha256(root / path)}
    preexisting = sorted(p for p, f in current.items()
                         if p in before and before[p]["status"] == f["status"] and before[p]["sha256"] == f["sha256"])
    for p in preexisting:
        del current[p]
    for p, f in before.items():  # uncommitted work that was there at the start and is gone now
        if p not in current and p not in preexisting and not _matches(p, ignore):
            current[p] = {"path": p, "status": "reverted_preexisting", "sha256": _sha256(root / p)}
    files, outside, denied = [], [], []
    for path in sorted(current):
        f = current[path]
        status = f["status"]
        files.append(f)
        hit = _matches(path, deny)
        if hit:
            denied.append({"path": path, "status": status, "rule": f"deny {hit}"})
        elif not _matches(path, allow):
            outside.append({"path": path, "status": status})

    problems = []
    if denied:
        problems.append(f"{len(denied)} change(s) to denied paths: " + ", ".join(d["path"] for d in denied[:5]))
    if outside:
        problems.append(f"{len(outside)} change(s) outside the allowed scope: " +
                        ", ".join(o["path"] for o in outside[:5]) + (" ..." if len(outside) > 5 else ""))
    limit = args.get("max_changed")
    too_many = isinstance(limit, int) and len(files) > limit
    if too_many:
        problems.append(f"{len(files)} files changed, more than max_changed {limit}")

    result = {"status": "fail" if problems else "pass", "base": base, "changed": files,
              "outside_scope": outside, "denied": denied}
    if baseline is not None:
        result["baseline"] = {"preexisting_unchanged": preexisting}
    if isinstance(limit, int):
        result["max_changed"] = limit
    result["detail"] = ("; ".join(problems) if problems else
                        f"{len(files)} changed file(s), all within {allow}")
    return result


SCOPE_DOCS = {
    "scope_changes": {
        "family": "repository",
        "verifies": ("every file changed in the git working tree against base (modified, added, "
                     "deleted, renamed, or new and not ignored) matches an allow glob and no deny glob. "
                     "Catches an agent that edits outside its task: a config, a lockfile, a test it "
                     "weakened, another module. Paths under .taskattest/ are never counted. Globs are "
                     "relative to root; * stays in one directory, ** crosses directories"),
        "fields": {"root": "directory to judge, relative to the spec; default the spec's directory. "
                           "For a spec in .taskattest/ use \"..\"",
                   "allow": "path globs the task may change, such as [\"src/billing/**\", \"tests/**\"]",
                   "deny": "optional globs that may never change even when allowed, such as [\"**/*.lock\"]",
                   "ignore": "optional globs left out of the judgment entirely",
                   "base": "optional git ref to compare against, default HEAD",
                   "max_changed": "optional upper bound on the number of changed files"},
        "baseline": ("with the completion gate, uncommitted work present when the session started is "
                     "recorded and left out; a pre-existing change the agent undid counts as a change"),
    },
}
