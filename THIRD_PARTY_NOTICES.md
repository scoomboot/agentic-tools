# Third-party notices

## modo-mcp 0.3.0

Some files in modo-tools are adapted from
modo-mcp 0.3.0 by Leonard
Billeke, under the MIT License below.

Adapted code (each file says so in its header):

- `kit/modo_tools/Scripts/python_modules/modotools/protocol.py` and
  `server/modo_tools/protocol.py` (from `mcpbridge/protocol.py`)
- `kit/modo_tools/Scripts/python_modules/modotools/pump.py` (from
  `mcpbridge/pump.py`)
- `kit/modo_tools/Scripts/python_modules/modotools/token.py` (from
  `mcpbridge/authtoken.py`)
- `kit/modo_tools/Scripts/python_modules/modotools/listener.py` (parts, from
  `mcpbridge/netserver.py`)
- `kit/modo_tools/Scripts/lxserv/modo_tools_cmds.py` (parts, from
  `lxserv/mcp_bridge_cmds.py`)
- `kit/modo_tools/Configs/modo_tools_menu.cfg` (parts, from
  `Configs/mcp_bridge_menu.cfg`)
- `server/modo_tools/bridge.py` (parts, from `modo_mcp/bridge.py`)

Other files take only ideas from modo-mcp and say so; their code is ours. No
code comes from modo-mcp's scene walker, channel reader or filter table,
which modo-mcp ported from ShaderTreeToUSD.

```
MIT License

Copyright (c) 2026 Leonard Billeke

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Foundry Modo

Modo is a product of Foundry. This repository holds no Foundry files: no
documentation, SDK headers or sample scripts. `mt_search_docs` builds its
index on the user's own PC from the Modo install found there.
