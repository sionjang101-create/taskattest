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

"""Protocol test for the MCP server: drives a real subprocess over stdio.

Covers both protocol eras, the three tools, the error contract (protocol errors
as JSON-RPC errors, tool failures as isError results), evidence replay, and
--root path confinement. No network, no model calls, no third-party packages.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from taskattest import mcp_server, runner
import selftest

ROOT = Path(__file__).parent.resolve()
SERVER_MODULE = "taskattest.mcp_server"
MODERN = "2026-07-28"
LEGACY = "2025-06-18"

META = {"io.modelcontextprotocol/protocolVersion": MODERN,
        "io.modelcontextprotocol/clientCapabilities": {}}

failures: list = []


def check(condition: bool, label: str) -> None:
    print(("OK  " if condition else "NG  ") + label)
    if not condition:
        failures.append(label)


class Server:
    """A taskattest MCP server running as a subprocess, spoken to over stdio."""

    def __init__(self, *args: str):
        self.log = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", SERVER_MODULE, *args],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log,
            cwd=str(ROOT), text=True, encoding="utf-8", bufsize=1)
        self.next_id = 0
        self.watchdog = threading.Timer(60.0, self.proc.kill)
        self.watchdog.daemon = True
        self.watchdog.start()

    def send(self, message: dict) -> None:
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def request(self, method: str, params: dict | None = None, meta: dict | None = None):
        self.next_id += 1
        params = dict(params or {})
        if meta is not None:
            params["_meta"] = meta
        message = {"jsonrpc": "2.0", "id": self.next_id, "method": method}
        if params:
            message["params"] = params
        self.send(message)
        line = self.proc.stdout.readline()
        if not line:
            raise AssertionError(f"server closed stdout while answering {method}\n"
                                 f"stderr:\n{self.stderr()}")
        reply = json.loads(line)  # fails loudly if stdout carries anything but MCP
        assert reply.get("id") == self.next_id, f"id mismatch on {method}: {reply}"
        return reply

    def notify(self, method: str, params: dict | None = None) -> None:
        message = {"jsonrpc": "2.0", "method": method}
        if params:
            message["params"] = params
        self.send(message)

    def stderr(self) -> str:
        self.log.seek(0)
        return self.log.read()

    def close(self) -> None:
        self.watchdog.cancel()
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()
        self.log.close()


def result_of(reply: dict, label: str) -> dict:
    if "error" in reply:
        raise AssertionError(f"{label}: unexpected protocol error {reply['error']}")
    return reply["result"]


def structured(reply: dict, label: str) -> dict:
    res = result_of(reply, label)
    assert res.get("isError") is False, f"{label}: tool reported an error: {res}"
    return res["structuredContent"]


# ---------- fixtures ----------

def build_fixtures() -> dict:
    return {name: selftest.build_case(name, produce)
            for name, produce in (("case_ok", "correct"),
                                  ("case_incomplete", "incomplete"))}


# ---------- tests ----------

def test_legacy(specs: dict) -> None:
    print("\n=== legacy handshake (initialize) ===")
    s = Server()
    try:
        res = result_of(s.request("initialize", {
            "protocolVersion": LEGACY,
            "capabilities": {},
            "clientInfo": {"name": "taskattest-test", "version": "1.0.0"}}), "initialize")
        check(res.get("protocolVersion") == LEGACY, "initialize echoes the requested version")
        check(res.get("serverInfo", {}).get("name") == mcp_server.SERVER_NAME,
              "serverInfo names the server")
        check("tools" in res.get("capabilities", {}), "server advertises the tools capability")
        check("resultType" not in res, "no resultType on the legacy path")

        s.notify("notifications/initialized")

        res = result_of(s.request("tools/list"), "tools/list")
        names = [t["name"] for t in res["tools"]]
        check(names == ["verify_task", "replay_evidence", "draft_spec", "describe_checks"],
              f"tools/list returns the four tools: {names}")
        check(all(isinstance(t.get("inputSchema"), dict) for t in res["tools"]),
              "every tool declares an inputSchema object")
        check("resultType" not in res, "no resultType on a legacy tools/list")

        out = structured(s.request("tools/call", {"name": "describe_checks", "arguments": {}}),
                         "describe_checks")
        check(set(out["check_types"]) == set(runner.CHECKS),
              f"describe_checks lists exactly the {len(runner.CHECKS)} implemented "
              f"check types: {len(out['check_types'])}")
        check("checks" in out["spec_template"]
              and "checks" in out["spec_template_deliverable"],
              "describe_checks returns both spec templates")

        reply = s.request("tools/call", {"name": "verify_task",
                                         "arguments": {"spec_path": str(specs["case_incomplete"])}})
        res = result_of(reply, "verify_task")
        out = res["structuredContent"]
        check(out["verdict"] == "partial", f"incomplete output is judged partial: {out['verdict']}")
        check(out["model_calls"] == 0, "no model calls")
        check(out["judged_by"] == "deterministic_rules_only", "judged by rules only")
        check(json.loads(res["content"][0]["text"]) == out,
              "the text block carries the same JSON as structuredContent")
    finally:
        s.close()


def test_modern(specs: dict) -> dict:
    print("\n=== modern handshake (server/discover) ===")
    s = Server()
    evidence_dir = ""
    try:
        res = result_of(s.request("server/discover", meta={
            "io.modelcontextprotocol/protocolVersion": MODERN,
            "io.modelcontextprotocol/clientInfo": {"name": "taskattest-test", "version": "1.0.0"},
            "io.modelcontextprotocol/clientCapabilities": {}}), "server/discover")
        check(res.get("resultType") == "complete", "discover result is complete")
        check(MODERN in res.get("supportedVersions", []),
              f"discover advertises {MODERN}: {res.get('supportedVersions')}")
        check(res.get("_meta", {}).get("io.modelcontextprotocol/serverInfo", {}).get("name")
              == mcp_server.SERVER_NAME, "discover carries serverInfo in _meta")

        res = result_of(s.request("tools/list", meta=META), "tools/list")
        check(res.get("resultType") == "complete", "modern tools/list carries resultType")

        reply = s.request("tools/call",
                          {"name": "verify_task", "arguments": {"spec_path": str(specs["case_ok"])}},
                          meta=META)
        res = result_of(reply, "verify_task")
        check(res.get("resultType") == "complete", "modern tools/call carries resultType")
        out = res["structuredContent"]
        check(out["verdict"] == "pass", f"correct output is judged pass: {out['verdict']}")
        evidence_dir = out["evidence_dir"]

        reply = s.request("tools/list")  # no _meta
        check(reply.get("error", {}).get("code") == -32602,
              f"modern client without _meta is rejected: {reply.get('error')}")

        reply = s.request("tools/list", meta={
            "io.modelcontextprotocol/protocolVersion": "1900-01-01",
            "io.modelcontextprotocol/clientCapabilities": {}})
        error = reply.get("error", {})
        check(error.get("code") == -32022, f"unsupported version is rejected: {error}")
        check(MODERN in error.get("data", {}).get("supported", []),
              "the rejection names the versions the server supports")
    finally:
        s.close()
    return {"evidence_dir": evidence_dir}


def test_errors(specs: dict) -> None:
    print("\n=== error contract ===")
    s = Server()
    try:
        s.request("initialize", {"protocolVersion": LEGACY, "capabilities": {},
                                 "clientInfo": {"name": "t", "version": "1"}})
        reply = s.request("tools/call", {"name": "no_such_tool", "arguments": {}})
        check(reply.get("error", {}).get("code") == -32602,
              f"unknown tool is a protocol error: {reply.get('error')}")

        reply = s.request("nonexistent/method")
        check(reply.get("error", {}).get("code") == -32601,
              f"unknown method is method-not-found: {reply.get('error')}")

        missing = ROOT / "fixtures" / "does_not_exist" / "spec.json"
        res = result_of(s.request("tools/call", {"name": "verify_task",
                                                 "arguments": {"spec_path": str(missing)}}),
                        "verify_task on a missing spec")
        check(res.get("isError") is True, "a missing spec is a tool error, not a protocol error")
        check("not found" in res["content"][0]["text"], "the tool error says what was wrong")
    finally:
        s.close()


def test_replay(evidence_dir: str) -> None:
    print("\n=== evidence replay ===")
    s = Server()
    try:
        s.request("initialize", {"protocolVersion": LEGACY, "capabilities": {},
                                 "clientInfo": {"name": "t", "version": "1"}})
        manifest = Path(evidence_dir) / "manifest.json"
        check(manifest.is_file(), f"the run left a manifest at {manifest}")
        out = structured(s.request("tools/call",
                                   {"name": "replay_evidence",
                                    "arguments": {"manifest_path": str(manifest)}}),
                         "replay_evidence")
        check(out["digest_match"] is True,
              f"replay reproduces the digest: {out['recorded_digest'][:12]} "
              f"vs {out['recomputed_digest'][:12]}")
        check(out["verdict_match"] is True, "replay reproduces the verdict")
    finally:
        s.close()


def test_root_confinement(specs: dict) -> None:
    print("\n=== --root confinement ===")
    confined = specs["case_ok"].parent  # only this case is permitted
    s = Server("--root", str(confined))
    try:
        s.request("initialize", {"protocolVersion": LEGACY, "capabilities": {},
                                 "clientInfo": {"name": "t", "version": "1"}})
        out = structured(s.request("tools/call",
                                   {"name": "verify_task",
                                    "arguments": {"spec_path": str(specs["case_ok"])}}),
                         "verify_task inside the root")
        check(out["verdict"] == "pass", "a spec inside the root still runs")

        res = result_of(s.request("tools/call",
                                  {"name": "verify_task",
                                   "arguments": {"spec_path": str(specs["case_incomplete"])}}),
                        "verify_task outside the root")
        check(res.get("isError") is True, "a spec outside the root is refused")
        check("outside the permitted root" in res["content"][0]["text"],
              "the refusal says the path left the root")
    finally:
        s.close()


def test_draft_tool(specs: dict) -> None:
    print("\n=== draft_spec over MCP ===")
    confined = specs["case_ok"].parent
    s = Server("--root", str(confined))
    try:
        s.request("initialize", {"protocolVersion": LEGACY, "capabilities": {},
                                 "clientInfo": {"name": "t", "version": "1"}})
        target = confined / "drafted_spec.json"
        if target.exists():
            target.unlink()
        out = structured(s.request("tools/call", {"name": "draft_spec", "arguments": {
            "spec_path": str(target),
            "inputs": {"orders": str(confined / "in" / "orders.csv")},
            "outputs": {"missing": str(confined / "out" / "missing_orders.csv")},
            "write": True}}), "draft_spec")
        check(out["spec"]["draft"] is True, "the drafted spec is marked draft")
        check(out["written_to"] and Path(out["written_to"]).is_file(), "write=true writes the file")
        kinds = {c["type"] for c in out["spec"]["checks"]}
        check({"file_hash", "subset_of", "unique_key"} <= kinds,
              f"seal, relation and uniqueness are proposed: {sorted(kinds)}")
        check("inputs.orders" in out["profiles"], "a field profile comes back for the agent")
        v = structured(s.request("tools/call", {"name": "verify_task",
                                                "arguments": {"spec_path": str(target)}}), "verify")
        check(v["warnings"] and "unreviewed draft" in v["warnings"][0],
              "verifying against the draft returns the warning")
        again = result_of(s.request("tools/call", {"name": "draft_spec", "arguments": {
            "spec_path": str(target), "write": True}}), "draft again")
        check(again.get("isError") is True and "refusing to overwrite" in again["content"][0]["text"],
              "drafting over an existing spec is refused")
        outside = result_of(s.request("tools/call", {"name": "draft_spec", "arguments": {
            "spec_path": str(confined / "x.json"),
            "inputs": {"o": str(specs["case_incomplete"].parent / "in" / "orders.csv")}}}),
            "draft outside root")
        check(outside.get("isError") is True and "outside the permitted root" in outside["content"][0]["text"],
              "draft_spec cannot read outside --root")
    finally:
        s.close()


def main() -> int:
    specs = build_fixtures()
    test_legacy(specs)
    modern = test_modern(specs)
    test_errors(specs)
    test_replay(modern["evidence_dir"])
    test_root_confinement(specs)
    test_draft_tool(specs)

    print("\n=== summary ===")
    if failures:
        for f in failures:
            print("FAILED:", f)
        return 1
    print("mcp protocol: both eras, 4 tools, error contract, replay, root confinement, draft - all pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
