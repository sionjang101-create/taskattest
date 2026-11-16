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

"""Scope check: changes outside the declared scope fail, with a fix that lists them.

Each case builds a throwaway git repository, changes files the way an agent
would, and checks the verdict, the paths reported and the fix. The last cases
run the completion gate, so an out-of-scope edit blocks the stop.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(ROOT))

from taskattest import gate, runner  # noqa: E402
from taskattest.mcp_server import ToolError, guard_spec_refs  # noqa: E402
from taskattest.scope import glob_regex  # noqa: E402

failures: list = []


def check(condition: bool, label: str) -> None:
    print(("OK  " if condition else "NG  ") + label)
    if not condition:
        failures.append(label)


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                    "-c", "commit.gpgsign=false", *args], cwd=str(repo), check=True,
                   capture_output=True)


def make_repo(base: Path) -> Path:
    repo = base / "repo"
    for rel, text in {"src/billing/invoice.py": "TOTAL = 1\n", "src/auth/login.py": "OK = True\n",
                      "tests/test_invoice.py": "def test(): pass\n", "package.lock": "v1\n",
                      "README.md": "# demo\n", ".gitignore": "build/\n"}.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8")
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    return repo


def spec(repo: Path, item: dict, where: str = ".taskattest") -> Path:
    d = repo / where
    d.mkdir(parents=True, exist_ok=True)
    s = {"task_id": "scope-demo", "checks": [dict({"type": "scope_changes", "name": "scope", "root": ".."}, **item)]}
    p = d / "spec.json"
    p.write_text(json.dumps(s), encoding="utf-8")
    return p


def run(repo: Path, item: dict) -> dict:
    m = runner.run(spec(repo, item), repo / ".taskattest" / "evidence")
    return m


def detail(m: dict) -> dict:
    return m["check_details"][0]


def result(m: dict) -> dict:
    return m["checks"][0]


def main() -> int:
    if shutil.which("git") is None:
        print("git is not available; the scope check needs it")
        return 1
    allow = {"allow": ["src/billing/**", "tests/**"]}

    print("=== glob semantics ===")
    check(bool(glob_regex("src/*.py").match("src/a.py")) and not glob_regex("src/*.py").match("src/x/a.py"),
          "* stays within one directory")
    check(bool(glob_regex("src/**").match("src/x/y/a.py")), "** crosses directories")
    check(bool(glob_regex("**/*.lock").match("package.lock")) and bool(glob_regex("**/*.lock").match("a/b.lock")),
          "**/ also matches zero directories")
    check(bool(glob_regex("./docs/*.md").match("docs/a.md")) and bool(glob_regex(".github/**").match(".github/x.yml")),
          "a leading ./ is dropped, but a leading dot in a name is kept")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        tmp = Path(tmp)
        repo = make_repo(tmp)

        print("\n=== clean and in-scope ===")
        m = run(repo, allow)
        check(m["verdict"] == "pass" and result(m)["changed"] == [],
              "a clean tree passes, and the spec and evidence under .taskattest/ are not counted")
        (repo / "src/billing/invoice.py").write_text("TOTAL = 2\n", encoding="utf-8")
        (repo / "tests/test_new.py").write_text("def test_new(): pass\n", encoding="utf-8")
        m = run(repo, allow)
        check(m["verdict"] == "pass" and len(result(m)["changed"]) == 2,
              f"modified and new files inside the scope pass: {[c['path'] for c in result(m)['changed']]}")
        first = m["evidence_digest"]
        check(run(repo, allow)["evidence_digest"] == first, "the same tree gives the same evidence digest")

        print("\n=== out of scope ===")
        (repo / "src/auth/login.py").write_text("OK = False\n", encoding="utf-8")
        (repo / "notes.txt").write_text("scratch\n", encoding="utf-8")
        (repo / "README.md").unlink()
        m = run(repo, allow)
        out = {o["path"]: o["status"] for o in result(m)["outside_scope"]}
        check(m["verdict"] != "pass" and out == {"src/auth/login.py": "modified", "notes.txt": "untracked",
                                                 "README.md": "deleted"},
              f"modified, new and deleted files outside the scope are each reported: {out}")
        check(result(m)["changed"][0]["sha256"] is not None or result(m)["changed"][0]["status"] == "deleted",
              "each changed file is recorded with its content hash")
        fix = detail(m)["fix"]
        how = {f["path"]: f["how"] for f in fix["files"]}
        check(fix["action"] == "revert_out_of_scope" and how["notes.txt"] == "delete it"
              and how["src/auth/login.py"] == "restore it from HEAD" and how["README.md"] == "restore it from HEAD",
              f"the fix says how to undo each one: {how}")
        check("do not edit the spec" in fix["note"], "the fix forbids widening the scope by editing the spec")
        check("src/billing/invoice.py" not in how, "in-scope changes are not in the fix")

        print("\n=== deny wins over allow ===")
        git(repo, "checkout", "-q", "--", "src/auth/login.py", "README.md")
        (repo / "notes.txt").unlink()
        (repo / "package.lock").write_text("v2\n", encoding="utf-8")
        m = run(repo, {"allow": ["**"], "deny": ["**/*.lock"]})
        check(m["verdict"] != "pass" and [d["path"] for d in result(m)["denied"]] == ["package.lock"],
              "a denied path fails even when allow covers everything")
        m = run(repo, {"allow": ["**"], "ignore": ["*.lock"]})
        check(m["verdict"] == "pass", "an ignored path is left out of the judgment")
        git(repo, "checkout", "-q", "--", "package.lock")

        print("\n=== bounds, gitignore, subdirectory root, unicode names ===")
        m = run(repo, dict(allow, max_changed=1))
        check(m["verdict"] != "pass" and "max_changed" in detail(m)["detail"],
              "more changed files than max_changed fails")
        (repo / "build").mkdir()
        (repo / "build" / "out.txt").write_text("built\n", encoding="utf-8")
        check(run(repo, allow)["verdict"] == "pass", "files git ignores are not counted")
        sub = runner.run(spec(repo, {"root": "..", "allow": ["invoice.py"]}, "src/billing/.taskattest"),
                         tmp / "ev")
        check(sub["verdict"] == "pass" and [c["path"] for c in result(sub)["changed"]] == ["invoice.py"],
              "with root set to a subdirectory, paths are relative to it and changes elsewhere are not judged")
        (repo / "src/billing/명세서.md").write_text("내용\n", encoding="utf-8")
        m = run(repo, allow)
        check(m["verdict"] == "pass" and "src/billing/명세서.md" in [c["path"] for c in result(m)["changed"]],
              "non-ASCII file names are reported as they are, not as quoted escapes")

        print("\n=== spec errors ===")
        m = run(repo, {"allow": []})
        check(m["verdict"] != "pass" and detail(m)["fix"]["action"] == "fix_spec", "an empty allow list is a spec error")
        m = run(repo, dict(allow, base="no-such-ref"))
        check(m["verdict"] != "pass" and detail(m)["fix"]["action"] == "fix_spec", "an unknown base ref is a spec error")
        plain = tmp / "plain"
        plain.mkdir()
        m = run(plain, allow)
        check(m["verdict"] != "pass" and detail(m)["fix"]["action"] == "fix_spec",
              f"outside a git repository the check fails as a spec error: {detail(m)['detail'][:60]}")

        print("\n=== MCP root guard ===")
        escape = spec(tmp / "guard", {"root": "../..", "allow": ["**"]})
        try:
            guard_spec_refs(escape, (tmp / "guard").resolve())
            refused = False
        except ToolError:
            refused = True
        check(refused, "a scope root outside the permitted --root is refused")

        print("\n=== baseline: the person's uncommitted work is not the agent's ===")
        (repo / "src/billing/명세서.md").unlink()
        (repo / "scratch.txt").write_text("person's notes\n", encoding="utf-8")
        (repo / "src/auth/login.py").write_text("OK = None  # person's edit in progress\n", encoding="utf-8")
        spec(repo, allow)
        started = gate.start(repo, "s1", "startup")
        recorded = json.loads((repo / ".taskattest/baselines/s1.json").read_text(encoding="utf-8"))
        recorded = next(iter(recorded["roots"].values()))["files"]
        check("baseline recorded" in started.get("systemMessage", "") and
              {"scratch.txt", "src/auth/login.py"} <= set(recorded),
              f"start records what is already dirty: {sorted(recorded)}")
        check(gate.evaluate(repo, session_id="s1").get("decision") is None,
              "pre-existing dirty files outside the scope do not block")
        check(gate.evaluate(repo, session_id="other").get("decision") == "block",
              "without that session's baseline the same tree blocks")
        (repo / "scratch.txt").write_text("person's notes\nagent added this\n", encoding="utf-8")
        again = gate.evaluate(repo, session_id="s1")
        check(again.get("decision") == "block" and "scratch.txt" in again.get("reason", ""),
              "a pre-existing file the agent changed further is judged as a change")
        (repo / "scratch.txt").write_text("person's notes\n", encoding="utf-8")
        git(repo, "checkout", "-q", "--", "src/auth/login.py")
        snap = json.loads((repo / ".taskattest/baselines/s1.json").read_text(encoding="utf-8"))["roots"]
        undone = runner.run(spec(repo, allow), repo / ".taskattest" / "evidence", snap)
        gone = [o for o in result(undone)["outside_scope"] if o["path"] == "src/auth/login.py"]
        check(undone["verdict"] != "pass" and bool(gone) and gone[0]["status"] == "reverted_preexisting",
              "discarding the person's uncommitted edit is reported as an out-of-scope change")
        (repo / "src/auth/login.py").write_text("OK = None  # person's edit in progress\n", encoding="utf-8")
        before = (repo / ".taskattest/baselines/s1.json").read_text(encoding="utf-8")
        (repo / "late.txt").write_text("x\n", encoding="utf-8")
        gate.start(repo, "s1", "resume")
        check((repo / ".taskattest/baselines/s1.json").read_text(encoding="utf-8") == before,
              "a resumed session keeps the baseline it started with")
        (repo / "late.txt").unlink()
        (repo / "scratch.txt").unlink()
        git(repo, "checkout", "-q", "--", "src/auth/login.py")
        (repo / ".taskattest" / "gate_state.json").unlink()

        print("\n=== completion gate ===")
        (repo / "src/auth/login.py").write_text("OK = False\n", encoding="utf-8")
        spec(repo, allow)
        blocked = gate.evaluate(repo)
        check(blocked.get("decision") == "block" and "src/auth/login.py" in blocked.get("reason", ""),
              "the gate blocks a stop after an out-of-scope edit and names the file")
        git(repo, "checkout", "-q", "--", "src/auth/login.py")
        done = gate.evaluate(repo)
        check("decision" not in done and "pass" in done.get("systemMessage", ""),
              "after the edit is undone the gate lets the agent stop")
        bundles = list((repo / ".taskattest" / "evidence").glob("*/*/manifest.json"))
        check(run(repo, allow)["verdict"] == "pass" and bundles,
              "the gate's own evidence and state files never count against the scope")

    if failures:
        for f in failures:
            print("FAILED:", f)
        return 1
    print("\nscope: in-scope passes, out-of-scope fails with an undo list, deny wins, the gate blocks")
    return 0


if __name__ == "__main__":
    os.environ.pop("TASKATTEST_SPEC", None)
    sys.exit(main())
