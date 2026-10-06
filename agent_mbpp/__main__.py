"""
MBPP Agent CLI (Section V.3 point 1).

Usage:
    uv run python -m agent_mbpp --task-file ../cache/mbpp_task.json \
        --output ../cache/mbpp_solution.json \
        --model-name "model/name" --provider-url "https://provider.api/v1"
    # or pick the provider from models.json (or omit both for its default):
    uv run python -m agent_mbpp --task-file ... --output ... \
        --provider gemini --model-name gemini-3.6-flash

STAGE 1 (current): connects to mcp_tools_mbpp.py over stdio, discovers its
  tools (run_tests), generates the sandbox manual from them, and enforces
  the hard limits (iterations/tokens/timeout). A watchdog guarantees a
  result file is written and the process stops before the time limit, even
  if a tool or a request hangs.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from common.agent_loop import AgentLoop, build_system_prompt
from common.env import load_env_file
from common.llm_provider import LLMProvider
from common.model_config import DEFAULT_MODELS_CONFIG, resolve_model
from common.models import MBPPTaskInput, SandboxConfig, SolutionOutput
from common.watchdog import ResultWriter, start_watchdog
from sandbox.executor import Sandbox, emergency_cleanup
from sandbox.manual import generate_sandbox_manual
from sandbox.mcp_client import MCPClient

# Hard limits (Section VI.1.1)
MAX_ITERATIONS = 10
MAX_INPUT_TOKENS = 6_000
MAX_OUTPUT_TOKENS = 1_500
TIMEOUT_SECONDS = 120

SAFETY_MARGIN_SECONDS = 10   # stop starting new work this early
WATCHDOG_MARGIN_SECONDS = 5  # hard stop this long before the limit


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="agent_mbpp")
    parser.add_argument("--task-file", required=True)
    parser.add_argument("--output", required=True)
    # Provider/model: either explicit (--model-name + --provider-url) or
    # looked up in models.json (--provider, or its "default"). See
    # common/model_config.py for the exact rules.
    parser.add_argument("--model-name", default=None)
    parser.add_argument("--provider-url", default=None)
    parser.add_argument("--provider", default=None, help="provider name from models.json")
    parser.add_argument("--api-key-env", default=None)
    parser.add_argument("--model-config", default=DEFAULT_MODELS_CONFIG)
    return parser.parse_args(argv)


def build_user_task(task: MBPPTaskInput) -> str:
    parts = [
        f"Task: {task.task_definition}",
        f"Function signature: {task.function_definition}",
    ]
    if task.test_imports:
        parts.append("Test imports:\n" + "\n".join(task.test_imports))
    parts.append("Tests:\n" + "\n".join(task.test_list))
    return "\n".join(parts)


def main(argv=None) -> None:
    args = parse_args(argv)
    load_env_file(".env")
    start = time.perf_counter()
    task_id = "unknown"
    mcp_client = MCPClient()
    writer = ResultWriter(args.output)
    state: dict = {"agent": None}

    def failure_result(error: str) -> SolutionOutput:
        return SolutionOutput(
            task_id=task_id,
            benchmark="mbpp",
            success=False,
            solution="",
            iterations=0,
            total_requests=0,
            total_input_tokens=0,
            total_output_tokens=0,
            total_time_seconds=time.perf_counter() - start,
            error=error,
        )

    def on_expire() -> None:
        agent = state["agent"]
        error = f"Hard time limit reached ({TIMEOUT_SECONDS}s), process stopped by watchdog"
        writer.write(agent.build_result(False, "", error) if agent else failure_result(error))
        # os._exit follows: stop sandbox workers and remove temp files first.
        emergency_cleanup()

    # Last line of defense: if anything hangs (a tool, a request, cleanup),
    # write whatever we have and stop the process before the limit.
    start_watchdog(TIMEOUT_SECONDS - WATCHDOG_MARGIN_SECONDS, start, on_expire)

    try:
        with open(args.task_file, encoding="utf-8") as f:
            task = MBPPTaskInput(**json.load(f))
        task_id = str(task.task_id)

        # run_tests() (inside mcp_tools_mbpp.py) needs to know which task is
        # currently being solved, so it can load the real test_list instead
        # of trusting whatever the LLM claims.
        os.environ["MBPP_TASK_FILE"] = args.task_file

        # Connect to the MBPP MCP server and discover what tools it exposes.
        mcp_client.connect_stdio("python mcp_tools_mbpp.py")
        mcp_client.discover_tools()
        # Resources and prompts are optional (Section V.2 point 5): a
        # tools-only server like ours yields none, an unknown server may not.
        mcp_client.discover_resources()
        mcp_client.discover_prompts()
        # Tool wrappers + the resource/prompt access functions, in one dict:
        # the Sandbox creates a proxy for every name in it.
        wrapped_tools = {
            **mcp_client.wrap_as_python_functions(),
            **mcp_client.wrap_resources_and_prompts(),
        }

        # Turn the discovered tool schemas into readable text so the system
        # prompt actually tells the LLM these tools exist and how to call them.
        sandbox_manual = generate_sandbox_manual(
            mcp_client.tools, mcp_client.resources, mcp_client.prompts
        )

        model_name, provider_url, api_key_env = resolve_model(
            args.model_config, args.provider, args.model_name, args.provider_url, args.api_key_env
        )
        provider = LLMProvider(model_name, provider_url, api_key_env)
        sandbox = Sandbox(config=SandboxConfig(), mcp_tools=wrapped_tools)
        system_prompt = build_system_prompt(sandbox_manual=sandbox_manual, benchmark="mbpp")

        agent = AgentLoop(
            llm_provider=provider,
            sandbox=sandbox,
            system_prompt=system_prompt,
            max_iterations=MAX_ITERATIONS,
            max_input_tokens=MAX_INPUT_TOKENS,
            max_output_tokens=MAX_OUTPUT_TOKENS,
            timeout_seconds=TIMEOUT_SECONDS,
            start_time=start,
            safety_margin_seconds=SAFETY_MARGIN_SECONDS,
        )
        state["agent"] = agent
        result = agent.run(task_id=task_id, benchmark="mbpp", user_task=build_user_task(task))
    except Exception as e:
        result = failure_result(f"{type(e).__name__}: {e}")

    # Write the result BEFORE attempting any cleanup, so a cleanup failure
    # (e.g. shutting down the MCP subprocess) can never cause us to lose a
    # task result we already have. ResultWriter writes only once, so the
    # watchdog cannot overwrite this later.
    writer.write(result)
    print(f"success={result.success} iterations={result.iterations} error={result.error}")

    try:
        # Shuts down the MCP session, the mcp_tools_mbpp.py subprocess, and
        # the background event-loop thread. Without this the process can
        # hang instead of exiting cleanly.
        mcp_client.close()
    except Exception as e:
        print(f"[warning] MCP client cleanup failed: {type(e).__name__}: {e}", file=sys.stderr)


if __name__ == "__main__":
    main(sys.argv[1:])