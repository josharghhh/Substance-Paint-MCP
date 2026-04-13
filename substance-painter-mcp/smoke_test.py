#!/usr/bin/env python3
import importlib.util
import json
import sys


def main() -> int:
    spec = importlib.util.spec_from_file_location(
        "sp_server", "/home/joshf/.codex/mcp/substance-painter-mcp/server.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.send_to_painter("get_project_status", {})
    print(json.dumps(result, indent=2))
    return 0 if "error" not in result else 1


if __name__ == "__main__":
    raise SystemExit(main())
