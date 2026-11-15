# modo-tools

An MCP server and a Modo kit that let an AI agent (Claude Code, or any MCP
client) work in Foundry Modo: run commands and Python, read the scene and
meshes, take screenshots, and undo its own work. Each agent call is one undo
step, an agent can roll back a whole action, and every call goes into a
journal you can replay.

> **Status.** Tested on **Windows with Modo 17.1v1** (Python 3.10 in Modo)
> only. Other Modo versions and macOS/Linux are not tested. On macOS the kit
> is likely to work, but the server's dialog and hang detection and the docs
> search paths are Windows-only. Expect rough edges.

## Read this first: security

`mt_run_python` runs **any Python code** inside Modo, with your user's rights:
it can read, write and delete your files. `mt_run_command` runs any Modo
command. Only connect agents and MCP clients that you trust.

What limits the risk:

- The kit listens on **127.0.0.1 only** (port 15680). It refuses any other
  address, so other computers on your network cannot connect.
- Each connection must send a random token. The kit writes it to
  `~/.modo_tools/token` on first start. Any program that runs as your user
  can read that file, so this stops other users and web pages, not your own
  programs.
- The kit does not start with Modo unless you tick **Start with Modo** in
  its menu.
- Set `MODO_TOOLS_ALLOW_PYTHON=0` in the server's environment to remove
  `mt_run_python`. Modo commands still run.

Save your scene before you let an agent work in it. Modo can crash (see
[Known Modo 17.1 hazards](#known-modo-171-hazards)).

## Install (Windows)

You need Modo 17.1 and Python 3.10 or newer (a normal Python install, not
Modo's own Python).

1. **Get the kit.** Download `modo_tools.lpk` from the
   [Releases](../../releases) page, or build it from a clone of this
   repository:

       python kit/modo_tools/build_lpk.py

   This writes `dist\modo_tools.lpk`.
2. **Remove an older kit.** If you installed modo-tools before, close Modo
   and delete the old kit folder first:

       Remove-Item -Recurse "$env:APPDATA\Luxology\Kits\modo_tools"

3. **Install the kit.** Drag `modo_tools.lpk` into a Modo viewport, click
   OK, and restart Modo. The kit loads only after a restart.
4. **Start the kit.** Click **Modo Tools > Start** at the end of Modo's menu
   bar, or type `modoTools.start` in Modo's command line. The event log says
   `modo-tools ready on 15680`. Tick **Modo Tools > Start with Modo** if you
   want the kit to start each time Modo starts.
5. **Install the server.** In a clone of this repository:

       python -m pip install .

   Use `pip install -e .` if you want to edit the code.
6. **Register the server with your MCP client.** The server speaks MCP over
   stdio. Use the full path of the Python from step 5.

   Claude Code, for all your projects:

       claude mcp add --scope user modo-tools -- C:\path\to\python.exe -m modo_tools

   Leave out `--scope user` to register it for one project only. Restart
   Claude Code to see the `mt_` tools.

   Other MCP clients: add a stdio server with the command
   `C:\path\to\python.exe` and the arguments `-m modo_tools`. For example, in
   a JSON config:

       "modo-tools": {
         "command": "C:\\path\\to\\python.exe",
         "args": ["-m", "modo_tools"]
       }

7. **Check it.** Ask the agent to call `mt_status`. Or, without an agent:

       python -m modo_tools call status

### Settings

Environment variables for the server:

| Variable | Default | What it does |
| --- | --- | --- |
| `MODO_TOOLS_PORT` | `15680` | The kit's port. Start the kit with `modoTools.start port:N` to match. |
| `MODO_TOOLS_ALLOW_PYTHON` | `1` | `0` removes `mt_run_python`. |
| `MODO_TOOLS_ANSWER_DIALOGS` | `1` | `0` leaves dialogs that open during a call for you to answer. |
| `MODO_TOOLS_MAX_CHARS` | `40000` | The largest answer, in characters. |
| `MODO_TOOLS_MODO_DIR` | newest `Modo*` install | The Modo folder that `mt_search_docs` indexes. |
| `MODO_TOOLS_HOME` | `~/.modo_tools` | Token, journals, op log and docs index. |

## The tools

| Tool | What it does |
| --- | --- |
| `mt_status` | Is Modo reachable; Modo, Python and kit versions. |
| `mt_run_command`, `mt_run_python` | Run a Modo command line, or Python, in Modo. One undo step per call. |
| `mt_begin`, `mt_commit`, `mt_rollback` | Group calls into an action that can be rolled back as one. |
| `mt_undo_last` | Undo the newest call. |
| `mt_command_info`, `mt_find_commands` | A command's real arguments and values; find commands by name. |
| `mt_search_docs`, `mt_read_doc` | Search Modo's Python API, its docs on your PC, and the Modo facts in this repo. |
| `mt_scene_summary`, `mt_scene_graph`, `mt_get_item`, `mt_get_selection`, `mt_set_channels`, `mt_save_scene` | Read and change the scene. |
| `mt_describe_mesh`, `mt_points`, `mt_set_points` | Read mesh facts and points; write points. |
| `mt_backdrop`, `mt_fit_backdrops` | Add reference images; fit a mesh to their silhouettes. |
| `mt_screenshot` | A picture of the viewport. |
| `mt_journal`, `mt_op_log`, `mt_event_log` | What the agent did (a replayable script), the last jobs (also after a crash), Modo's event log. |

## Known Modo 17.1 hazards

These crashed or froze Modo 17.1 in our tests. The server refuses the
commands marked "refused" until the agent saves and sends
`acknowledge_risk=true`. The full list, with details, is in
[`server/modo_tools/docs/modo_facts.md`](server/modo_tools/docs/modo_facts.md),
which agents can search with `mt_search_docs`.

- **`scene.open` from a script froze Modo** (refused). Open scenes yourself
  from File > Open.
- **`deformer.freeze`, `mesh.psubSubdiv`, `mesh.psubRenderSubdiv`** crashed
  Modo in scripted builds (refused).
- **Adding polygons and setting SubD edge weights in the same call** crashed
  Modo twice. Do them in separate calls.
- **Reading the hints of `tool.attr`** froze Modo; reading the popup of
  `scene.tagAdd` crashed it. `mt_command_info` skips both.
- **`QApplication.processEvents()` inside a kit job** crashed Modo.
- **Holding Ctrl+Z** past the `scene.new` step that made the scene crashed
  Modo.
- `scene.new`, `scene.close`, `item.delete`, `viewport.fit` and a few more
  can lock Modo when Python mesh operations are in the scene. The server
  runs them with a warning.

Crash dumps go to `%LOCALAPPDATA%\CrashDumps`. After a crash or a freeze,
`mt_op_log` shows the last jobs, even with Modo closed.

## Layout

    kit/modo_tools/      the kit Modo loads (build_lpk.py makes the .lpk)
    server/modo_tools/   the MCP server (`python -m modo_tools`)
    tests/               a fake lx module, and the tests

## Tests

    python -m unittest discover -s tests

The kit's tests need only the standard library. One server test needs the
`mcp` package and is skipped without it.

## Licence

MIT, see [LICENSE](LICENSE). Some files are adapted from modo-mcp 0.3.0
(MIT); see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). Modo is a
product of Foundry. This project is not made or endorsed by Foundry.
