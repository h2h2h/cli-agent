"""Fixed stdlib-only worker executed by a Workspace Tool venv."""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        code = payload["code"]
        workspace = Path(payload["workspace"])
        cwd = Path(payload["cwd"])
        tools_directory = Path(payload["tools_directory"])
        tool_paths = payload["tool_paths"]
        if not isinstance(code, str) or not isinstance(tool_paths, dict):
            raise TypeError("invalid Tool worker payload")
    except Exception as exc:
        print(f"WorkerInputError: {exc}", file=sys.stderr)
        return 1

    os.chdir(cwd)
    sys.path.insert(0, str(tools_directory))
    tools = SimpleNamespace()
    for name, raw_path in sorted(tool_paths.items()):
        try:
            path = Path(raw_path)
            spec = importlib.util.spec_from_file_location(
                f"cli_agent_tool_{name}",
                path,
            )
            if spec is None or spec.loader is None:
                raise ImportError(f"cannot create import spec for {path}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            setattr(tools, name, module)
        except Exception as exc:
            print(
                f"Warning: Failed to load tool {name}: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )

    namespace = {
        "__name__": "__main__",
        "__builtins__": __builtins__,
        "cwd": cwd,
        "workspace": workspace,
        "tools_dir": tools_directory,
        "tools_directory": tools_directory,
        "tools": tools,
        "ast": ast,
        "json": json,
        "os": os,
        "re": re,
        "sys": sys,
        "Path": Path,
    }
    try:
        _execute_repl_style(code, namespace)
    except SyntaxError as exc:
        print(f"SyntaxError: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


def _execute_repl_style(code: str, namespace: dict[str, object]) -> None:
    tree = ast.parse(code, filename="<tools run>")
    if not tree.body:
        return

    last = tree.body[-1]
    if not isinstance(last, ast.Expr):
        exec(compile(tree, "<tools run>", "exec"), namespace)
        return

    if len(tree.body) > 1:
        statements = ast.Module(body=tree.body[:-1], type_ignores=[])
        exec(compile(statements, "<tools run>", "exec"), namespace)
    result = eval(
        compile(ast.Expression(last.value), "<tools run>", "eval"),
        namespace,
    )
    if result is not None:
        print(result)


if __name__ == "__main__":
    raise SystemExit(main())
