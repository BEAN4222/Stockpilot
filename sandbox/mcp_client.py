"""
MCP Client (Section III.1 diagram + Section V.2 point 5).

The sandbox itself acts as an MCP client, connecting to an MCP server
(could be our own mcp_tools_mbpp.py / mcp_tools_swebench.py, or, during
evaluation, an unknown MCP server — so this logic must stay generic and
never hardcode "I know there are exactly these tools").

What gets exposed (Section V.2 point 5: "MCP tools, resources, and prompts
must be exposed")
--------------------------------------------------------------------------
An MCP server can offer three kinds of things:
  - tools:     functions the LLM can call (read_file, run_tests, ...)
  - resources: read-only data, each identified by a URI (config://..., file://...)
  - prompts:   reusable prompt templates that take string arguments
All three are discovered here. Tools become one Python function each (see
wrap_as_python_functions). Resources and prompts are exposed through four
generic functions (see wrap_resources_and_prompts): list_resources(),
read_resource(uri=...), list_prompts() and get_prompt(name=..., arguments=...).
Many servers (ours included) offer only tools, so resources and prompts
are optional: a server that does not support them simply yields none,
and nothing extra appears in the sandbox.

Sync vs async — why the background thread exists
--------------------------------------------------
The official MCP Python SDK is async (built on asyncio): every call to the
server (listing tools, calling a tool) is an `await`-able coroutine. But the
sandbox's exec(code) runs plain synchronous code — when the LLM's code calls
read_file(...), it needs a normal blocking function call that returns a
value, not something it has to `await`.

To bridge this, we start a background thread that runs its own asyncio
event loop for the lifetime of the connection. Sandbox functions schedule
async calls onto that loop and *block* until the result comes back, so from
the sandbox's point of view it's an ordinary synchronous function call.

Why the session lives inside ONE persistent task
--------------------------------------------------
anyio (which the MCP SDK is built on) requires that a cancel scope — which
is what `async with stdio_client(...)` / `async with ClientSession(...)`
open under the hood — be entered AND exited from the *same* asyncio Task.

`asyncio.run_coroutine_threadsafe(coro, loop)` wraps each call in a brand
new Task. If we open the connection in one such call and close it in
another, the open and close happen in two different Tasks, and anyio raises
"Attempted to exit cancel scope in a different task than it was entered
in". So instead of opening/closing across separate scheduled calls, the
entire session lifetime (open -> wait -> close) runs inside a single
long-lived coroutine/Task (`_session_lifecycle`). Individual tool calls are
separate, self-contained tasks that only *use* the already-open session —
they never touch the enter/exit of its cancel scope, so they're safe to
run as their own short-lived tasks.
"""
from __future__ import annotations

import asyncio
import os
import shlex
import threading
from typing import Callable, Dict, Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client


class MCPClient:
    def __init__(self):
        self.tools: Dict[str, object] = {}  # tool_name -> Tool schema object (from the server)
        self.resources: Dict[str, object] = {}  # uri -> Resource object (from the server)
        self.prompts: Dict[str, object] = {}  # prompt_name -> Prompt object (from the server)
        # What the server said it supports during initialize(). Used to skip
        # asking for resources/prompts when the server does not offer them.
        self._capabilities = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._session: Optional[ClientSession] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._ready = threading.Event()  # signalled once the session is initialized (or failed)
        self._connect_error: Optional[BaseException] = None

    # ------------------------------------------------------------------
    # Connecting
    # ------------------------------------------------------------------
    def connect_stdio(self, command: str) -> None:
        """Launch the MCP server as a subprocess and connect over stdin/stdout.

        `command` example: "python mcp_tools_mbpp.py"
        """
        # shlex (not str.split) so a quoted path with spaces stays one argument.
        parts = shlex.split(command)
        # Pass the full environment explicitly: without `env`, the MCP SDK
        # only forwards a small whitelist (PATH, HOME, ...) to the server
        # subprocess, so variables like MBPP_TASK_FILE would be dropped.
        server_params = StdioServerParameters(
            command=parts[0], args=parts[1:], env=dict(os.environ)
        )
        self._start(lambda: stdio_client(server_params))

    def connect_http(self, url: str) -> None:
        """Connect to an MCP server that is ALREADY running and listening
        on `url` (streamable HTTP transport), e.g. "http://127.0.0.1:8000/mcp".

        Unlike stdio, nothing is launched here: the server's lifetime is
        independent of ours, so close() only ends our session with it.
        """
        self._start(lambda: streamable_http_client(url))

    def _start(self, open_transport: Callable) -> None:
        """Run the session lifecycle on a background event loop and block
        until it is ready (or failed). Shared by both transports.
        """

        def run_loop():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._session_lifecycle(open_transport))

        self._thread = threading.Thread(target=run_loop, daemon=True)
        self._thread.start()

        # Block the calling (sync) thread until the background task has
        # either finished initializing the session, or failed to.
        self._ready.wait()
        if self._connect_error is not None:
            raise self._connect_error

    async def _session_lifecycle(self, open_transport: Callable) -> None:
        """Owns the ENTIRE lifetime of the connection: open, stay open
        while tool calls happen elsewhere, then close — all within this
        one task, which is what anyio's cancel-scope rule requires.

        `open_transport()` returns the transport's async context manager.
        This is the ONLY part that differs between stdio and HTTP: both
        yield a (read, write, ...) tuple of streams — HTTP adds a third
        item, a session-id getter we don't need — and everything after
        that (ClientSession, initialize, list_tools, call_tool) is the same.
        """
        self._stop_event = asyncio.Event()
        try:
            async with open_transport() as streams:
                read, write = streams[0], streams[1]
                async with ClientSession(read, write) as session:
                    init_result = await session.initialize()
                    # The server's answer to initialize() lists what it
                    # supports (tools / resources / prompts ...). Keep it so
                    # discover_resources()/discover_prompts() know whether
                    # asking makes sense at all.
                    self._capabilities = getattr(init_result, "capabilities", None)
                    self._session = session
                    self._ready.set()  # connect_*() can now return
                    await self._stop_event.wait()  # keep the session open until close() is called
        except BaseException as e:
            # anyio wraps failures in an ExceptionGroup ("unhandled errors in
            # a TaskGroup"); unwrap single-error groups to show the real cause.
            while len(getattr(e, "exceptions", ())) == 1:
                e = e.exceptions[0]
            self._connect_error = e
            self._ready.set()
        finally:
            self._session = None

    # ------------------------------------------------------------------
    # Discovering and wrapping tools
    # ------------------------------------------------------------------
    def _run_coro(self, coro):
        """Schedule `coro` as its own short-lived task on the background
        loop and block until it's done. Safe to call for anything that
        only USES the already-open session (list_tools, call_tool) —
        never for opening/closing the session itself.
        """
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result()

    def discover_tools(self) -> Dict[str, object]:
        async def _list():
            result = await self._session.list_tools()
            return result.tools

        tools = self._run_coro(_list())
        self.tools = {t.name: t for t in tools}
        return self.tools

    def wrap_as_python_functions(self) -> Dict[str, Callable]:
        """Turn every discovered MCP tool into a plain Python function that
        can be dropped straight into the sandbox's exec() namespace.

        NOTE: wrapped functions only accept KEYWORD arguments (e.g.
        read_file(filepath="a.py", start_line=1, end_line=10)), never
        positional ones. The system prompt should tell the LLM to always
        call tools with keyword arguments.
        """
        return {name: self._make_wrapper(name) for name in self.tools}

    def _make_wrapper(self, tool_name: str) -> Callable:
        def wrapper(**kwargs):
            async def _call():
                return await self._session.call_tool(tool_name, arguments=kwargs)

            result = self._run_coro(_call())
            # MCP tool results are a list of content blocks; join the text ones.
            texts = [block.text for block in result.content if hasattr(block, "text")]
            return "\n".join(texts)

        wrapper.__name__ = tool_name
        return wrapper

    # ------------------------------------------------------------------
    # Discovering resources and prompts
    # ------------------------------------------------------------------
    def discover_resources(self) -> Dict[str, object]:
        """List the server's resources (read-only data identified by a URI).

        Returns {} when the server does not offer resources. A server that
        advertises them but fails to list them is treated the same way, so
        an odd server can never stop the agent from starting.
        """
        if not getattr(self._capabilities, "resources", None):
            self.resources = {}
            return self.resources

        async def _list():
            result = await self._session.list_resources()
            return result.resources

        try:
            resources = self._run_coro(_list())
        except Exception:
            resources = []
        self.resources = {str(r.uri): r for r in resources}
        return self.resources

    def discover_prompts(self) -> Dict[str, object]:
        """List the server's prompt templates.

        Returns {} when the server does not offer prompts (or fails to list
        them), for the same reason as discover_resources().
        """
        if not getattr(self._capabilities, "prompts", None):
            self.prompts = {}
            return self.prompts

        async def _list():
            result = await self._session.list_prompts()
            return result.prompts

        try:
            prompts = self._run_coro(_list())
        except Exception:
            prompts = []
        self.prompts = {p.name: p for p in prompts}
        return self.prompts

    def wrap_resources_and_prompts(self) -> Dict[str, Callable]:
        """Plain Python functions that give the sandbox access to the
        server's resources and prompts, merged with the tool wrappers by
        the caller before the Sandbox is created.

        Only added when the server actually offers resources / prompts, so
        a tools-only server (like ours) adds nothing to the sandbox. Like
        the tool wrappers, they are called through the sandbox's proxies,
        so they take KEYWORD arguments only, and always return a string
        (errors included) instead of raising.
        """
        functions: Dict[str, Callable] = {}
        if self.resources:
            functions["list_resources"] = self._list_resources
            functions["read_resource"] = self._read_resource
        if self.prompts:
            functions["list_prompts"] = self._list_prompts
            functions["get_prompt"] = self._get_prompt
        # A real tool with the same name always wins: never hide it.
        return {name: fn for name, fn in functions.items() if name not in self.tools}

    def _list_resources(self) -> str:
        """One line per resource: its URI, then its description (or name)."""
        lines = []
        for uri, resource in self.resources.items():
            description = getattr(resource, "description", None) or getattr(resource, "name", "") or ""
            lines.append(f"{uri}  {description}".rstrip())
        return "\n".join(lines) or "(no resources)"

    def _read_resource(self, uri: str) -> str:
        """Return the content of the resource at `uri` as text."""

        async def _read():
            return await self._session.read_resource(uri)

        try:
            result = self._run_coro(_read())
        except Exception as e:
            return f"[error] read_resource({uri!r}) failed: {type(e).__name__}: {e}"

        parts = []
        for content in result.contents:
            if getattr(content, "text", None) is not None:
                parts.append(content.text)
            else:
                # Binary content (an image, a PDF, ...) cannot be shown as text.
                mime_type = getattr(content, "mimeType", None) or "unknown type"
                parts.append(f"[binary content, {mime_type}]")
        return "\n".join(parts)

    def _list_prompts(self) -> str:
        """One line per prompt: name(arguments) then its description.
        Optional arguments are marked with a trailing '?'."""
        lines = []
        for name, prompt in self.prompts.items():
            arguments = getattr(prompt, "arguments", None) or []
            signature = ", ".join(a.name + ("" if a.required else "?") for a in arguments)
            description = getattr(prompt, "description", None) or ""
            lines.append(f"{name}({signature})  {description}".rstrip())
        return "\n".join(lines) or "(no prompts)"

    def _get_prompt(self, name: str, arguments: Optional[dict] = None) -> str:
        """Fill the prompt template `name` with `arguments` and return its
        messages, one per line, prefixed with their role."""
        # The MCP spec defines prompt arguments as strings.
        string_arguments = {key: str(value) for key, value in (arguments or {}).items()}

        async def _get():
            return await self._session.get_prompt(name, arguments=string_arguments)

        try:
            result = self._run_coro(_get())
        except Exception as e:
            return f"[error] get_prompt({name!r}) failed: {type(e).__name__}: {e}"

        lines = []
        for message in result.messages:
            text = getattr(message.content, "text", None)
            lines.append(f"[{message.role}] {text if text is not None else '(non-text content)'}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------
    def close(self) -> None:
        """Signal `_session_lifecycle` to exit its `async with` blocks
        (closing the session and the subprocess from within the same task
        that opened them), then stop the background loop and thread.
        """
        if not self._loop or not self._stop_event:
            return

        self._loop.call_soon_threadsafe(self._stop_event.set)
        if self._thread:
            self._thread.join(timeout=10)