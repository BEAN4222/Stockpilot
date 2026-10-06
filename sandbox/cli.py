"""
Sandbox CLI entry point (Section V.2 point 1).

Wired up in pyproject.toml as:
    [project.scripts]
    sandbox = "sandbox.cli:main"

Usage (examples from the subject PDF):
    uv run sandbox                                             # interactive REPL
    uv run sandbox sandbox_template.json                       # custom config
    uv run sandbox --mcp-stdio "python mcp_tools_mbpp.py" sandbox_template.json
    uv run sandbox --mcp-server <URL>

When an MCP server is given, everything it offers is exposed in the REPL
(Section V.2 point 5): its tools as functions, plus list_resources() /
read_resource(uri=...) and list_prompts() / get_prompt(name=..., arguments=...)
when the server offers resources or prompts. The generated manual is
printed first, so you can see exactly what the LLM would be told.
"""
from __future__ import annotations

import argparse
import json
import sys

from common.models import SandboxConfig
from sandbox.executor import Sandbox
from sandbox.manual import generate_sandbox_manual
from sandbox.mcp_client import MCPClient


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="sandbox")
    parser.add_argument(
        "config_file", nargs="?", default=None, help="path to a sandbox config JSON (e.g. sandbox_template.json)"
    )
    parser.add_argument(
        "--mcp-stdio", dest="mcp_stdio", default=None, help='command that starts the MCP server, e.g. "python mcp_tools_mbpp.py"'
    )
    parser.add_argument(
        "--mcp-server", dest="mcp_server", default=None, help="URL of an MCP server that is already running (streamable HTTP)"
    )
    return parser.parse_args(argv)


def load_config(config_file) -> SandboxConfig:
    if config_file is None:
        return SandboxConfig()
    with open(config_file) as f:
        data = json.load(f)
    return SandboxConfig(**data)


def main(argv=None) -> None:
    args = parse_args(argv)
    if args.mcp_stdio and args.mcp_server:
        sys.exit("error: --mcp-stdio and --mcp-server cannot be used together")
    try:
        config = load_config(args.config_file)
    except FileNotFoundError:
        sys.exit(f"error: cannot find the file: {args.config_file}")
    except json.JSONDecodeError as e:
        sys.exit(f"error: JSON error: {e}")
    except Exception as e:
        sys.exit(f"error: config error: {e}")

    # stdio: we launch the server ourselves. HTTP: it must already be running.
    mcp_client = MCPClient()
    mcp_functions = {}
    if args.mcp_stdio or args.mcp_server:
        try:
            if args.mcp_stdio:
                mcp_client.connect_stdio(args.mcp_stdio)
            else:
                mcp_client.connect_http(args.mcp_server)
            mcp_client.discover_tools()
            # Resources and prompts are optional: a tools-only server yields none.
            mcp_client.discover_resources()
            mcp_client.discover_prompts()
        except Exception as e:
            mcp_client.close()
            sys.exit(f"error: cannot connect to MCP server: {type(e).__name__}: {e}")
        # Tool wrappers + the resource/prompt access functions, all in one
        # dict: the Sandbox creates a proxy for every name in it.
        mcp_functions = {
            **mcp_client.wrap_as_python_functions(),
            **mcp_client.wrap_resources_and_prompts(),
        }
        print(generate_sandbox_manual(mcp_client.tools, mcp_client.resources, mcp_client.prompts))

    try:
        # Must come after discovery: the worker's function names are fixed here.
        sandbox = Sandbox(config=config, mcp_tools=mcp_functions)
        try:
            sandbox.run_repl()
        finally:
            sandbox.shutdown()
    finally:
        mcp_client.close()


if __name__ == "__main__":
    main(sys.argv[1:])