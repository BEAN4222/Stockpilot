"""
Code extraction layer (Section V.1, "Extract LLM-generated Python code").

The LLM's response is a block of text, for example:

    Let me check the definition of this function first.
```python
    result = search_code("is_valid_email")
    print(result)
```
    <end_code>

We pull the executable code out of this text, across the different formats
the LLM might produce, and turn it into a single Python code string that can
be handed to the sandbox.

Supported formats:
1. Python code blocks (primary): ```python ... ``` <end_code>
2. XML tool calls (Anthropic-style): <invoke name="..."><parameter>...</parameter></invoke>
3. JSON/Hermes tool calls: <tool_call>{"name": "...", "arguments": {...}}</tool_call>
4. ReAct format: Action: tool_name / Action Input: {...}
5. Function-tag tool calls: <function=name><parameter=key>value</parameter></function>

Non-Python formats are converted into equivalent Python function call
strings, printed so the result reaches the LLM, e.g. a non-Python call to
read_file becomes:
    print(read_file(filepath="/testbed/file.py"))

If a single response mixes several formats, the extracted snippets are
concatenated in the order they appear in the original text.

Feedback to the LLM (V.1: "The LLM should never be left guessing"):
whenever the extraction had to interpret something, `warning` says exactly
what was done, and the agent loop shows it to the model:
  - a non-Python format was converted: the converted Python is quoted
  - a ```python block had no closing ```: the code was recovered anyway
  - a JSON entry could not be parsed and was skipped
  - a ReAct Action Input was not valid JSON, so the call got no arguments
  - nothing usable was found: the message says how to reply instead
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple


@dataclass
class ExtractionResult:
    code: Optional[str]          # extracted executable Python code; None if nothing found
    raw_llm_output: str          # raw LLM output (stored in StepMetrics.llm_output)
    warning: Optional[str] = None  # what was interpreted or what went wrong, shown to the LLM


# ---------------------------------------------------------------------------
# Regex patterns for each format
# ---------------------------------------------------------------------------

_PY_BLOCK_RE = re.compile(r"```python\s*(.*?)```\s*(?:<end_code>)?", re.DOTALL)

# A ```python block that was opened but never closed (e.g. the reply was cut off).
_PY_OPEN_RE = re.compile(r"```python[ \t]*\n?(.*)\Z", re.DOTALL)
_END_CODE_TAIL_RE = re.compile(r"\s*<end_code>\s*\Z")

_XML_INVOKE_RE = re.compile(r'<invoke name="([^"]+)">(.*?)</invoke>', re.DOTALL)
_XML_PARAM_RE = re.compile(r'<parameter name="([^"]+)">(.*?)</parameter>', re.DOTALL)

_JSON_TOOLCALL_RE = re.compile(r'<tool_call>\s*(\{.*?\})\s*</tool_call>', re.DOTALL)

# Function-tag calls, which some models fall back to (usually inside <tool_call>):
#   <function=read_file>
#   <parameter=filepath>
#   /testbed/x.py
#   </parameter>
#   </function>
_FUNCTION_TAG_RE = re.compile(r"<function=([^>\s]+)>(.*?)</function>", re.DOTALL)
_FUNCTION_TAG_PARAM_RE = re.compile(r"<parameter=([^>\s]+)>(.*?)</parameter>", re.DOTALL)

# The lookahead accepts either a newline followed by another
# Action/Observation block, OR the end of the string directly
# (no trailing newline required in the latter case).
_REACT_RE = re.compile(
    r'Action:\s*(\S+)\s*\nAction Input:\s*(\{.*?\})(?=\n(?:Action:|Observation:)|$)',
    re.DOTALL,
)

_NO_CODE_MESSAGE = (
    "No code block found. Reply with 'Thought: ...' followed by a "
    "```python ... ``` block, then <end_code>."
)

_MAX_SHOWN_CONVERTED_CHARS = 300


# ---------------------------------------------------------------------------
# Internal helpers (not part of the public API)
# ---------------------------------------------------------------------------

def _coerce(val: str) -> Any:
    """Try to recover the real type of an XML parameter value
    (number / bool / list / dict, etc). Falls back to the raw string
    if that fails.
    """
    try:
        return json.loads(val)
    except json.JSONDecodeError:
        return val


def _call_to_python(name: str, arguments: dict) -> str:
    """Convert a (function name, arguments dict) pair into a single
    line of equivalent Python function-call code. Uses repr() instead
    of manual string building to avoid quoting/escaping bugs.

    The call is wrapped in print(): only printed output reaches the LLM,
    and a tool call is made to see its result. final_answer returns None,
    so printing it would only add noise.
    """
    args_str = ", ".join(f"{k}={v!r}" for k, v in arguments.items())
    call = f"{name}({args_str})"
    return call if name == "final_answer" else f"print({call})"


def _extract_xml_calls(text: str) -> List[Tuple[int, str]]:
    """Extract Anthropic-style XML tool calls."""
    results = []
    for m in _XML_INVOKE_RE.finditer(text):
        name, body = m.group(1), m.group(2)
        args = {
            p.group(1): _coerce(p.group(2).strip())
            for p in _XML_PARAM_RE.finditer(body)
        }
        results.append((m.start(), _call_to_python(name, args)))
    return results


def _extract_json_calls(text: str, notes: List[str]) -> List[Tuple[int, str]]:
    """Extract JSON/Hermes-style <tool_call>{...}</tool_call> calls."""
    results = []
    skipped = 0
    for m in _JSON_TOOLCALL_RE.finditer(text):
        try:
            data = json.loads(m.group(1))
            results.append(
                (m.start(), _call_to_python(data["name"], data.get("arguments", {})))
            )
        except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
            # Malformed entry: skip it rather than failing the whole extraction,
            # but remember it so the LLM is told.
            skipped += 1
    if skipped:
        notes.append(
            f"{skipped} <tool_call> block(s) could not be parsed (invalid JSON or no "
            f"'name') and were skipped, nothing was run for them."
        )
    return results


def _extract_function_tag_calls(text: str, notes: List[str]) -> List[Tuple[int, str]]:
    """Extract <function=name><parameter=key>value</parameter></function> calls."""
    results = []
    for m in _FUNCTION_TAG_RE.finditer(text):
        name = m.group(1)
        # Values sit on their own lines; drop only that one leading/trailing
        # newline, never the indentation an edit_file old_str depends on.
        args = {
            p.group(1): _coerce(re.sub(r"\A\n|\n\Z", "", p.group(2)))
            for p in _FUNCTION_TAG_PARAM_RE.finditer(m.group(2))
        }
        # Some replies wrap every call in a generic function whose parameters
        # are the real tool's name and its arguments: unwrap it.
        if "tool_name" in args:
            name = str(args["tool_name"]).strip()
            inner = args.get("arguments", {})
            if not isinstance(inner, dict):
                notes.append(
                    f"The arguments of '{name}' were not a JSON object, so I called "
                    f"{name}() with no arguments."
                )
                inner = {}
            args = inner
        results.append((m.start(), _call_to_python(name, args)))
    return results


def _extract_react_calls(text: str, notes: List[str]) -> List[Tuple[int, str]]:
    """Extract ReAct-format calls: Action: xxx / Action Input: {...}"""
    results = []
    for m in _REACT_RE.finditer(text):
        name = m.group(1).strip()
        try:
            args = json.loads(m.group(2))
        except json.JSONDecodeError:
            args = {}
            notes.append(
                f"The Action Input of '{name}' was not valid JSON, so I called "
                f"{name}() with no arguments."
            )
        results.append((m.start(), _call_to_python(name, args)))
    return results


# ---------------------------------------------------------------------------
# Single public entry point
# ---------------------------------------------------------------------------

def extract_python_code_block(llm_text: str) -> ExtractionResult:
    """Extract executable Python code from a raw LLM response.

    Detects all five formats, sorts the matches by their position in
    the original text, and concatenates them into one code string.
    The agent loop only needs to call this one function.
    """
    events: List[Tuple[int, str]] = []
    notes: List[str] = []

    # 1) Properly closed ```python blocks.
    last_end = 0
    for m in _PY_BLOCK_RE.finditer(llm_text):
        events.append((m.start(), m.group(1).strip()))
        last_end = m.end()

    # 2) A ```python block that was opened but never closed.
    open_match = _PY_OPEN_RE.search(llm_text[last_end:])
    if open_match:
        code = _END_CODE_TAIL_RE.sub("", open_match.group(1)).strip()
        if code:
            events.append((last_end + open_match.start(), code))
            notes.append(
                "Your ```python block had no closing ```, so I ran everything after "
                "it. If your reply was cut off, the code may be incomplete: keep "
                "your answers shorter."
            )

    # 3) Other formats, converted to Python.
    converted: List[str] = []
    formats: List[str] = []
    for label, found in (
        ("XML", _extract_xml_calls(llm_text)),
        ("JSON", _extract_json_calls(llm_text, notes)),
        ("function-tag", _extract_function_tag_calls(llm_text, notes)),
        ("ReAct", _extract_react_calls(llm_text, notes)),
    ):
        if found:
            formats.append(label)
            events.extend(found)
            converted.extend(snippet for _, snippet in found)
    if converted:
        shown = "; ".join(converted)
        if len(shown) > _MAX_SHOWN_CONVERTED_CHARS:
            shown = shown[:_MAX_SHOWN_CONVERTED_CHARS] + "..."
        notes.append(
            f"Your reply used {'/'.join(formats)} tool-call syntax instead of a "
            f"```python block. I converted it into Python and ran: {shown}. "
            f"Next time write a ```python ... ``` block followed by <end_code>."
        )

    if not events:
        extra = (" " + " ".join(notes)) if notes else ""
        return ExtractionResult(
            code=None,
            raw_llm_output=llm_text,
            warning=_NO_CODE_MESSAGE + extra,
        )

    events.sort(key=lambda e: e[0])
    code = "\n".join(snippet for _, snippet in events)
    return ExtractionResult(
        code=code,
        raw_llm_output=llm_text,
        warning=" ".join(notes) if notes else None,
    )