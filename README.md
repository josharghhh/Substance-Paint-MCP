# Substance Painter MCP Share Bundle

This folder is the extracted Substance Painter MCP setup that was previously only stored in the archive:

- `/mnt/z/Documents/My Games/PeerPlugin1/substance-painter-mcp-and-skill-full-with-edits-2026-04-05.zip`

It has been turned into a clean git repository at the bundle root so the full deliverable can be versioned together:

- `substance-painter-mcp/` contains the MCP server and Painter plugin
- `substance-painter-mcp-master/` contains the Codex skill and reference docs
- `BUNDLE_NOTES.txt` preserves the original archive note about included edits

## Where The Setup Was

The working setup was not unpacked anywhere in this workspace. It lived only inside these zip files:

- `substance-painter-mcp-and-skill-full-with-edits-2026-04-05.zip`
- `substance-painter-mcp-and-skill-2026-04-05.zip`

The `full-with-edits` archive is the important one because it records that the MCP repo had local edits when it was bundled.

## Quick Start

1. Copy `substance-painter-mcp/painter_plugin.py` into the Substance Painter plugins folder.
2. Restart Substance Painter, then enable `Python > painter_plugin`.
3. Install the Python dependency for the MCP server:

```bash
pip install mcp
```

4. Point your MCP client at:

```text
substance-painter-mcp/server.py
```

5. If you want the accompanying Codex skill, install or copy:

```text
substance-painter-mcp-master/
```

into your Codex skills directory.

## Bundle Layout

- `substance-painter-mcp/README.md`: upstream-style server and plugin setup guide
- `substance-painter-mcp/server.py`: MCP stdio server
- `substance-painter-mcp/painter_plugin.py`: Substance Painter plugin that exposes the TCP bridge
- `substance-painter-mcp/smoke_test.py`: local smoke test script
- `substance-painter-mcp-master/SKILL.md`: Codex skill definition
- `substance-painter-mcp-master/references/`: Painter workflow references
- `docs/SETUP.md`: repo-level setup and packaging notes for this extracted bundle

## Git Notes

This repository was initialized from the extracted bundle so it can be committed and pushed independently of the rest of the workspace.

Suggested next commands:

```bash
git remote add origin <your-repo-url>
git add .
git commit -m "Import Substance Painter MCP share bundle"
git push -u origin main
```
