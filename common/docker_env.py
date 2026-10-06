"""
Docker container lifecycle management (Section V.4).

Pulled out of agent_swebench/__main__.py so it can be reused: agent_swebench
uses it to run real evaluation tasks, and if the sandbox CLI (`uv run
sandbox --mcp-stdio ...`) ever needs to spin up its own container for manual
debugging, it can just import this module instead of duplicating the
pull/start/cleanup logic.

The three functions map to the three stages of a container's life: pull the
image -> start the container -> clean it up when done. Everything calls the
docker CLI with argument lists (never a shell string), so each argument is
passed straight to execve -- no escaping to worry about, no shell-injection
risk.
"""
from __future__ import annotations

import subprocess
import sys

# Official SWE-bench images are built for x86_64 (the image name itself says
# so: swebench/sweb.eval.x86_64...). On an Apple Silicon dev machine, not
# passing this flag can pull a broken manifest or produce odd compatibility
# issues; forcing amd64 means running under emulation (a bit slower), but
# that matters less than matching the grading machine's (x86_64) behavior,
# so it's on by default.
DOCKER_PLATFORM = "linux/amd64"

# `docker stop` waits 10s for a graceful SIGTERM exit by default before
# SIGKILL. These containers are disposable, so there's no reason to wait
# that long -- cutting it to 2s makes cleanup noticeably faster.
DOCKER_STOP_TIMEOUT_SECONDS = 2


def container_name(instance_id: str) -> str:
    """Docker container names must match [a-zA-Z0-9][a-zA-Z0-9_.-]*.
    SWE-bench instance_ids (e.g. 'sympy__sympy-14711') already satisfy that;
    this just namespaces them so they don't collide with unrelated
    containers on the host."""
    return f"agent-smith-{instance_id}"

def qualify_image(image: str) -> str:
    first = image.split("/", 1)[0]
    if "/" in image and ("." in first or ":" in first or first == "localhost"):
        return image
    return f"docker.io/{image}"

def pull_image(image: str) -> None:
    """Pull the image the task needs. Lets the exception propagate on
    failure -- if the image can't be pulled, nothing downstream can work
    either, so there's no point making this best-effort."""
    image = qualify_image(image)
    if subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode == 0:
        return
    subprocess.run(
        ["docker", "pull", "--platform", DOCKER_PLATFORM, image],
        check=True,
        stdout=sys.stderr,
    )


def start_container(image: str, name: str) -> None:
    """Start a detached container running /bin/bash so it stays alive;
    every MCP tool later reaches into this container via `docker exec`."""
    image = qualify_image(image)
    # Best-effort: force-remove any stale container left over from a
    # previous crashed run before starting a fresh one under the same
    # name. Deliberately not check=True -- the container most likely
    # doesn't exist yet, and that "failure" is the normal case, so it
    # shouldn't crash the program at this step.
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    subprocess.run(
        [
            "docker", "run", "-dit",
            "--platform", DOCKER_PLATFORM,
            "--network", "none",
            "--name", name,
            image, "/bin/bash",
        ],
        check=True,
        stdout=sys.stderr,
    )


def cleanup_container(name: str) -> None:
    """Stop + remove the task container. Best-effort, never raises -- by
    the time this is called the result has already been written to disk,
    and a cleanup failure should never cost us a result we already have."""
    subprocess.run(
        ["docker", "stop", "-t", str(DOCKER_STOP_TIMEOUT_SECONDS), name],
        capture_output=True,
    )
    subprocess.run(["docker", "rm", name], capture_output=True)
