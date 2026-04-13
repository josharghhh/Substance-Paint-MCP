# Substance Painter MCP Setup Notes

## What This Bundle Contains

This share bundle packages two separate pieces that belong together:

- `substance-painter-mcp/`: the actual MCP implementation
- `substance-painter-mcp-master/`: the Codex skill and reference workflow docs

The MCP implementation itself has two runtime sides:

- `server.py`: a stdio MCP server that your AI client launches
- `painter_plugin.py`: a Substance Painter plugin that runs inside Painter and listens locally

## Source Of Truth

The extracted folder came from:

- `/mnt/z/Documents/My Games/PeerPlugin1/substance-painter-mcp-and-skill-full-with-edits-2026-04-05.zip`

That archive included a note that the MCP repo had uncommitted edits when it was bundled. The original note is preserved in [BUNDLE_NOTES.txt](/mnt/z/Documents/My Games/PeerPlugin1/substance-painter-share-bundle-full/BUNDLE_NOTES.txt).

## Install The Painter Side

Copy [painter_plugin.py](/mnt/z/Documents/My Games/PeerPlugin1/substance-painter-share-bundle-full/substance-painter-mcp/painter_plugin.py) into the Substance Painter plugins folder:

- Windows: `%USERPROFILE%\Documents\Adobe\Adobe Substance 3D Painter\python\plugins\`
- macOS: `~/Documents/Adobe/Adobe Substance 3D Painter/python/plugins/`
- Linux: `~/Documents/Adobe/Adobe Substance 3D Painter/python/plugins/`

After copying it:

1. Fully restart Substance Painter.
2. Open Painter.
3. Enable `Python > painter_plugin`.

## Install The MCP Server Side

Use Python 3.10+ and install the MCP package:

```bash
pip install mcp
```

Your MCP client should launch:

```text
/absolute/path/to/substance-painter-mcp/server.py
```

The main runtime path in this extracted bundle is:

```text
/mnt/z/Documents/My Games/PeerPlugin1/substance-painter-share-bundle-full/substance-painter-mcp/server.py
```

## Client Wiring

For any stdio-compatible MCP client, the config pattern is:

```json
{
  "mcpServers": {
    "substance-painter": {
      "command": "python",
      "args": [
        "/absolute/path/to/substance-painter-mcp/server.py"
      ]
    }
  }
}
```

The server then talks to Painter on local port `9877`.

## Codex Skill Packaging

The bundle also includes a reusable Codex skill:

- [SKILL.md](/mnt/z/Documents/My Games/PeerPlugin1/substance-painter-share-bundle-full/substance-painter-mcp-master/SKILL.md)
- [openai.yaml](/mnt/z/Documents/My Games/PeerPlugin1/substance-painter-share-bundle-full/substance-painter-mcp-master/agents/openai.yaml)

To reuse it in another Codex environment, copy `substance-painter-mcp-master/` into that environment's skills directory.

## Git Packaging Choice

The zip contained an embedded `.git` directory only for `substance-painter-mcp/`. That is not ideal if you want to version the server and the skill together.

This extracted folder was set up as a new clean git repository at the bundle root so that:

- the MCP server and Painter plugin stay together
- the Codex skill ships with the server
- the original bundle note remains tracked
- future docs can live at the repo root instead of inside the zip

## Recommended Next Step

If you want this published, add a remote and push this folder as its own repository:

```bash
cd /mnt/z/Documents/My Games/PeerPlugin1/substance-painter-share-bundle-full
git remote add origin <your-repo-url>
git add .
git commit -m "Import Substance Painter MCP bundle and skill"
git push -u origin main
```
