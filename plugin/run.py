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

"""Entry point for the Claude Code plugin: run taskattest from the plugin folder.

The plugin ships the package source, so nothing is installed: this script puts
the plugin root on sys.path and dispatches.

    run.py gate          Stop hook
    run.py gate start    SessionStart hook
    run.py mcp [--root]  MCP server (stdio)
    run.py ...           the taskattest CLI
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if sys.version_info < (3, 10):
    # A hook must never trap the agent: say why and let it stop.
    if sys.argv[1:2] == ["gate"]:
        print('{"systemMessage": "taskattest needs Python 3.10 or newer; set the plugin\'s python option. '
              'The gate did not run."}')
        sys.exit(0)
    sys.exit("taskattest needs Python 3.10 or newer")

args = sys.argv[1:]
if args[:1] == ["mcp"]:
    from taskattest import mcp_server
    sys.exit(mcp_server.main(args[1:]))
from taskattest import runner  # noqa: E402
sys.exit(runner.main(args))
