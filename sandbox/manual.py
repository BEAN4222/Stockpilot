"""
Sandbox manual (Section V.2 point 6).

This turns what an MCP server offers into a human-readable block of text
that gets embedded in the system prompt, so the LLM actually knows what
exists and how to use it. It must be generated DYNAMICALLY from whatever
MCP server we're connected to — if we swap in a different server, this
text should automatically reflect it, never be hardcoded.

Inputs (all from MCPClient after discovery):
  - tools:     {tool_name: Tool}, each with .name, .description and
               .inputSchema (a JSON Schema dict)
  - resources: {uri: Resource}, each with .uri, .name, .description, .mimeType
  - prompts:   {prompt_name: Prompt}, each with .name, .description and
               .arguments (a list of PromptArgument: .name, .description, .required)
Resources and prompts are optional: most servers offer only tools, and
then their sections are simply left out.
"""
from __future__ import annotations

from typing import Dict, Optional

_JSON_TYPE_TO_PY = {
    "string": "str",
    "integer": "int",
    "number": "float",
    "boolean": "bool",
    "array": "list",
    "object": "dict",
}


def _description_lines(obj: object) -> list:
    """The object's description, indented under its entry, blank lines dropped."""
    description = (getattr(obj, "description", "") or "").strip()
    return [f"  {line.strip()}" for line in description.splitlines() if line.strip()]


def _tools_section(tools: Dict[str, object]) -> list:
    lines = ["Available tools:", ""]
    for name, tool in tools.items():
        schema = getattr(tool, "inputSchema", None) or {}
        properties = schema.get("properties", {})
        required = set(schema.get("required", []))

        params = []
        for prop_name, prop_schema in properties.items():
            py_type = _JSON_TYPE_TO_PY.get(prop_schema.get("type"), "Any")
            if prop_name in required:
                params.append(f"{prop_name}: {py_type}")
            else:
                params.append(f"{prop_name}: {py_type} = None")

        lines.append(f"- {name}({', '.join(params)})")
        lines.extend(_description_lines(tool))
        lines.append("")
    return lines


def _resources_section(resources: Dict[str, object]) -> list:
    # Resources are not functions: tell the LLM how to reach them, with
    # keyword arguments, since the sandbox only accepts those.
    lines = [
        "Available resources (read-only data):",
        'Read one with read_resource(uri="..."), list them with list_resources().',
        "",
    ]
    for uri, resource in resources.items():
        mime_type = getattr(resource, "mimeType", None)
        lines.append(f"- {uri}" + (f" ({mime_type})" if mime_type else ""))
        name = getattr(resource, "name", None)
        if name and name != uri:
            lines.append(f"  name: {name}")
        lines.extend(_description_lines(resource))
        lines.append("")
    return lines


def _prompts_section(prompts: Dict[str, object]) -> list:
    lines = [
        "Available prompt templates:",
        'Fill one with get_prompt(name="...", arguments={"argument": "value"}), '
        "list them with list_prompts(). Argument values are strings.",
        "",
    ]
    for name, prompt in prompts.items():
        arguments = getattr(prompt, "arguments", None) or []
        params = [
            f"{a.name}: str" if a.required else f"{a.name}: str = None" for a in arguments
        ]
        lines.append(f"- {name}({', '.join(params)})")
        lines.extend(_description_lines(prompt))
        for argument in arguments:
            if getattr(argument, "description", None):
                lines.append(f"    {argument.name}: {argument.description.strip()}")
        lines.append("")
    return lines


def generate_sandbox_manual(
    tools: Dict[str, object],
    resources: Optional[Dict[str, object]] = None,
    prompts: Optional[Dict[str, object]] = None,
) -> str:
    resources = resources or {}
    prompts = prompts or {}
    if not tools and not resources and not prompts:
        return "(no extra tools connected)"

    lines: list = []
    if tools:
        lines += _tools_section(tools)
    if resources:
        lines += _resources_section(resources)
    if prompts:
        lines += _prompts_section(prompts)
    return "\n".join(lines).rstrip() + "\n"