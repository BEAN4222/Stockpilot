"""
SWE-bench Agent CLI (Section V.4 point 1).

Usage:
    uv run python -m agent_swebench --task-file ../cache/swebench_task.json \
        --output ../cache/swebench_solution.json \
        --model-name "model/name" --provider-url "https://provider.api/v1"
    # or pick the provider from models.json (or omit both for its default):
    uv run python -m agent_swebench --task-file ... --output ... \
        --provider gemini --model-name gemini-3.6-flash

Mirrors agent_mbpp/__main__.py: same AgentLoop / build_system_prompt /
SolutionOutput plumbing (see that file for the parts that are identical).
What's different here is the Docker container lifecycle -- Plan B: the
sandbox/agent loop stay on the host, mcp_tools_swebench.py bridges into the
task's container via `docker exec` instead of running inside it. So this
file additionally has to:
  1. pull the task's docker_image and start it detached
  2. tell mcp_tools_swebench.py which container to exec into, and where the
     task file (eval_script etc.) is, via environment variables -- same
     pattern as MBPP_TASK_FILE in agent_mbpp/__main__.py
  3. always clean the container up afterwards (stop + rm), success or not --
     exam_swebench.sh grades "container cleanup" as its own step

Time limit: the clock starts here (so pulling the image counts). On timeout
the agent tries to salvage the current patch, and a watchdog guarantees the
result file is written and the container removed before the limit.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from common.agent_loop import AgentLoop, build_system_prompt
from common.docker_env import cleanup_container, container_name, pull_image, start_container
from common.env import load_env_file
from common.llm_provider import LLMProvider
from common.model_config import DEFAULT_MODELS_CONFIG, resolve_model
from common.models import SandboxConfig, SolutionOutput, SWEBenchTaskInput
from common.watchdog import ResultWriter, run_with_timeout, start_watchdog
from sandbox.executor import Sandbox, emergency_cleanup
from sandbox.manual import generate_sandbox_manual
from sandbox.mcp_client import MCPClient

# Hard limits -- Section VI.1.2
MAX_ITERATIONS = 30
MAX_INPUT_TOKENS = 300_000
MAX_OUTPUT_TOKENS = 10_000
TIMEOUT_SECONDS = 900

SAFETY_MARGIN_SECONDS = 60    # stop starting new work this early
WATCHDOG_MARGIN_SECONDS = 10  # hard stop this long before the limit

# Env vars mcp_tools_swebench.py reads to know which task/container it is
# serving -- must match the constants of the same name over there.
TASK_FILE_ENV = "SWEBENCH_TASK_FILE"
CONTAINER_NAME_ENV = "SWEBENCH_CONTAINER_NAME"


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="agent_swebench")
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


def build_user_task(task: SWEBenchTaskInput) -> str:
    parts = []
    if task.repo:
        parts.append(f"Repository: {task.repo}")
    parts.append(f"Instance: {task.instance_id}")
    parts.append(f"Issue:\n{task.problem_statement}")
    if task.hints_text:
        parts.append(f"Hints:\n{task.hints_text}")
    return "\n\n".join(parts)


def main(argv=None) -> None:
    args = parse_args(argv)
    load_env_file(".env")
    start = time.perf_counter()
    task_id = "unknown"
    mcp_client = MCPClient()
    container: str | None = None
    writer = ResultWriter(args.output)
    state: dict = {"agent": None}

    def failure_result(error: str) -> SolutionOutput:
        return SolutionOutput(
            task_id=task_id,
            benchmark="swebench",
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
        # The process is about to be killed, so remove the container now
        # (with its own time cap, so this cannot hang the watchdog).
        if container is not None:
            run_with_timeout(lambda: cleanup_container(container), 6)

    # Last line of defense: if anything hangs (image pull, a tool, a request),
    # write whatever we have, remove the container, and stop the process.
    start_watchdog(TIMEOUT_SECONDS - WATCHDOG_MARGIN_SECONDS, start, on_expire)

    try:
        with open(args.task_file, encoding="utf-8") as f:
            task = SWEBenchTaskInput(**json.load(f))
        task_id = task.instance_id

        container = container_name(task.instance_id)
        pull_image(task.docker_image)
        start_container(task.docker_image, container)

        # mcp_tools_swebench.py needs to know which container to `docker
        # exec` into, and where the task file (eval_script, repo, ...) is --
        # same idea as MBPP_TASK_FILE in agent_mbpp/__main__.py.
        os.environ[TASK_FILE_ENV] = args.task_file
        os.environ[CONTAINER_NAME_ENV] = container

        mcp_client.connect_stdio("python mcp_tools_swebench.py")
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

        sandbox_manual = generate_sandbox_manual(
            mcp_client.tools, mcp_client.resources, mcp_client.prompts
        )

        model_name, provider_url, api_key_env = resolve_model(
            args.model_config, args.provider, args.model_name, args.provider_url, args.api_key_env
        )
        provider = LLMProvider(model_name, provider_url, api_key_env)
        sandbox = Sandbox(config=SandboxConfig(), mcp_tools=wrapped_tools)
        system_prompt = build_system_prompt(sandbox_manual=sandbox_manual, benchmark="swebench")

        def salvage() -> str:
            """On timeout, return the current diff so work already done is not lost."""
            patch = wrapped_tools["get_patch"]()
            text = str(patch) if patch is not None else ""
            return "" if text.startswith("[error]") else text

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
            salvage=salvage,
        )
        state["agent"] = agent
        result = agent.run(task_id=task_id, benchmark="swebench", user_task=build_user_task(task))
    except Exception as e:
        result = failure_result(f"{type(e).__name__}: {e}")

    # Write the result BEFORE attempting any cleanup, so a cleanup failure
    # (MCP subprocess or Docker) can never cause us to lose a task result we
    # already have. ResultWriter writes only once, so the watchdog cannot
    # overwrite this later.
    writer.write(result)
    print(f"success={result.success} iterations={result.iterations} error={result.error}")

    try:
        # Shuts down the MCP session, the mcp_tools_swebench.py subprocess,
        # and the background event-loop thread.
        mcp_client.close()
    except Exception as e:
        print(f"[warning] MCP client cleanup failed: {type(e).__name__}: {e}", file=sys.stderr)

    if container is not None:
        cleanup_container(container)


if __name__ == "__main__":
    main(sys.argv[1:])