# Painter Screenshot Flow

Treat screenshots as checkpoints in the workflow.

## When To Capture

Capture a screenshot:
- before making a meaningful visual change
- after rebuilding a base stack
- after the first material-response pass
- after any Blender-to-Painter reimport
- after fixing a suspected UV/material-slot issue
- before export when the user needs a sign-off view

## What To Show

For each screenshot, prefer:
- one medium framing view showing overall read
- one close-up on the problem area

Capture the view that answers the current question:
- “Does the base color still read correctly?”
- “Is the plush response soft or crunchy?”
- “Did the shards disappear?”
- “Did the export candidate still preserve the intended look?”

## Review Questions

Ask yourself:
- does the asset still read as the same object/character?
- does the current surface response match the intended material?
- is the problem geometric, UV-related, or purely textural?
- did the latest change improve the result or just change it?

## Shot Labels

Name screenshots clearly:
- `before_base_stack_fix`
- `after_base_stack_fix`
- `after_felt_pass`
- `after_mesh_reload`
- `after_export_validation`

## MCP Practicality

If no dedicated Painter viewport capture tool exists:
- bring Painter to foreground
- capture the desktop with Painter visible
- verify the capture actually contains Painter before using it as review evidence
