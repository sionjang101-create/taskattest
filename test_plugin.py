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

"""Claude Code plugin: the manifests agree with the package, and the launcher works.

Runs plugin/run.py the way the plugin's hooks and MCP entry do: the gate with
and without a spec, the start hook, and an MCP handshake. Loading the plugin
inside Claude Code itself is not covered here; see the README.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
RUN = ROOT / "plugin" / "run.py"
failures: list = []


def check(condition: bool, label: str) -> None:
    print(("OK  " if condition else "NG  ") + label)
    if not condition:
        failures.append(label)


def launch(args, cwd: Path, stdin: str = "") -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(RUN), *args], cwd=str(cwd), input=stdin,
                          capture_output=True, text=True, encoding="utf-8", timeout=60)


def main() -> int:
    sys.path.insert(0, str(ROOT))
    from taskattest import __version__

    print("=== manifests ===")
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    check(plugin["name"] == "taskattest" and plugin["version"] == __version__,
          f"plugin.json version matches the package: {plugin['version']} == {__version__}")
    check([p["name"] for p in market["plugins"]] == ["taskattest"] and market["plugins"][0]["source"] == "./",
          "the repository is its own marketplace, listing the plugin at its root")
    mcp = plugin["mcpServers"]["taskattest"]
    check(mcp["command"] == "${user_config.python}" and "python" in plugin["userConfig"]
          and mcp["args"][:2] == ["${CLAUDE_PLUGIN_ROOT}/plugin/run.py", "mcp"]
          and "--root" in mcp["args"],
          "the MCP entry runs the launcher with the configured Python and confines it to the project")
    commands = {event: [h["command"] for group in groups for h in group["hooks"]] for event, groups in hooks.items()}
    check(set(commands) == {"SessionStart", "Stop"}, f"hooks cover session start and stop: {sorted(commands)}")
    check(commands["SessionStart"][0].endswith("run.py\" gate start") and commands["Stop"][0].endswith("run.py\" gate")
          and all("CLAUDE_PLUGIN_OPTION_PYTHON" in c for cs in commands.values() for c in cs),
          "both hooks call the launcher with the configured Python")
    skill = (ROOT / "skills" / "define-done" / "SKILL.md").read_text(encoding="utf-8")
    head = skill.split("---")[1] if skill.startswith("---") else ""
    check("name: define-done" in head and "description:" in head, "the skill has a name and a trigger description")
    check("never edit it" in skill and "draft" in skill, "the skill forbids editing the spec and says drafts need review")

    print("\n=== launcher ===")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        tmp = Path(tmp)
        event = json.dumps({"hook_event_name": "Stop", "cwd": str(tmp), "session_id": "t1"})
        out = launch(["gate"], tmp, event)
        check(out.returncode == 0 and out.stdout.strip() == "", "with no spec the Stop hook prints nothing and allows")
        out = launch(["gate", "start"], tmp, json.dumps({"hook_event_name": "SessionStart", "cwd": str(tmp),
                                                         "session_id": "t1", "source": "startup"}))
        check(out.returncode == 0 and out.stdout.strip() == "", "with no spec the start hook does nothing")
        (tmp / ".taskattest").mkdir()
        (tmp / ".taskattest" / "spec.json").write_text(json.dumps({"task_id": "t", "checks": [
            {"type": "file_exists", "name": "report", "target": "../report.txt"}]}), encoding="utf-8")
        out = launch(["gate"], tmp, event)
        reply = json.loads(out.stdout) if out.stdout.strip() else {}
        check(out.returncode == 0 and reply.get("decision") == "block" and "report.txt" in reply.get("reason", ""),
              "a failing spec blocks through the launcher")
        (tmp / "report.txt").write_text("done\n", encoding="utf-8")
        reply = json.loads(launch(["gate"], tmp, event).stdout)
        check("decision" not in reply and "pass" in reply.get("systemMessage", ""), "a passing spec lets it stop")

        init = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                           "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                      "clientInfo": {"name": "t", "version": "1"}}})
        out = launch(["mcp", "--root", str(tmp)], tmp, init + "\n")
        first = json.loads(out.stdout.splitlines()[0]) if out.stdout.strip() else {}
        check(first.get("result", {}).get("serverInfo", {}).get("version") == __version__,
              "the launcher starts the MCP server, confined to the project")
        out = launch(["--help"], tmp)
        check(out.returncode == 0 and "--spec" in out.stdout, "other arguments reach the taskattest CLI")

    if failures:
        for f in failures:
            print("FAILED:", f)
        return 1
    print("\nplugin: manifests agree with the package; hooks, MCP and CLI run through the launcher")
    return 0


if __name__ == "__main__":
    sys.exit(main())
