"""
A throwaway MCP server used only for testing (not part of the project).

Its tools have nothing to do with MBPP or SWE-bench: if the sandbox can
discover them, describe them in the manual and call them, then nothing in
our code is hardcoded to our own tool names -- which is what the evaluation
checks by plugging in an MCP server we have never seen.

It also offers a resource and a prompt, the two other things an MCP server
can expose (Section V.2 point 5). Our own servers only have tools, so this
is the server to use to check that resources and prompts reach the sandbox:
they should show up in the manual, and be usable through
list_resources() / read_resource(uri=...) and list_prompts() /
get_prompt(name=..., arguments=...).

Run it like our real servers:
    uv run sandbox --mcp-stdio "python fake_mcp_server.py"
"""
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("fake-tools")


@mcp.tool()
def shout(text: str) -> str:
    """Return the given text in upper case with an exclamation mark."""
    return text.upper() + "!"


@mcp.tool()
def add_numbers(a: int, b: int) -> str:
    """Add two integers and return the result."""
    return f"{a} + {b} = {a + b}"


@mcp.tool()
def favourite_colour() -> str:
    """Return this server's favourite colour. Takes no arguments."""
    return "teal"


@mcp.resource("config://fake-settings")
def fake_settings() -> str:
    """Example read-only settings, to test resource discovery and reading."""
    return "max_retries = 3\nlanguage = en"


@mcp.prompt()
def review_code(language: str, focus: str = "bugs") -> str:
    """Ask for a code review in a given language, to test prompt templates."""
    return f"Please review this {language} code, focusing on {focus}."


if __name__ == "__main__":
    mcp.run()