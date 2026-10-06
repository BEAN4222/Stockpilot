"""
Agent loop core (Section V.1): Thought -> Code -> Observation.

    messages = [system_prompt, user_task]
    for step in 1..max_iterations:
        response    = llm.generate(messages, stop=["<end_code>"])
        code        = extract_python_code_block(response.text)
        observation = sandbox.execute(code)       (or a "no code found" message)
        if final_answer() was called -> return SolutionOutput
        messages += [assistant: response, user: "Observation: ..."]

STAGE 1 (current): max_iterations, max_input_tokens, max_output_tokens and
  timeout_seconds are all enforced. Token limits are cumulative across all
  iterations of the task (Section VI.1).

  Time limit: the clock starts at `start_time` (the entry point's start, so
  setup such as pulling an image counts). New work stops `safety_margin_seconds`
  before the limit, and every LLM request gets that same deadline, so one slow
  request cannot run past it. On timeout, `salvage` (if given) may return a
  partial solution, e.g. the current patch.

"""
from __future__ import annotations

import time
from typing import Callable, List, Optional

from common.code_extraction import extract_python_code_block
from common.llm_provider import LLMProvider
from common.models import SolutionOutput, StepMetrics
from common.watchdog import run_with_timeout

STOP_SEQUENCES = ["<end_code>"]


class AgentLoop:
    """Shared by the MBPP and SWE-bench agents; only the system prompt,
    the task text and the sandbox's MCP tools differ.
    """

    def __init__(
        self,
        llm_provider: LLMProvider,
        sandbox,  # sandbox.executor.Sandbox
        system_prompt: str,
        max_iterations: int,
        max_input_tokens: int,
        max_output_tokens: int,
        timeout_seconds: int,
        start_time: Optional[float] = None,
        safety_margin_seconds: float = 0.0,
        salvage: Optional[Callable[[], str]] = None,
    ):
        self.llm_provider = llm_provider
        self.sandbox = sandbox
        self.system_prompt = system_prompt
        self.max_iterations = max_iterations
        self.max_input_tokens = max_input_tokens
        self.max_output_tokens = max_output_tokens
        self.timeout_seconds = timeout_seconds
        self.start_time = start_time
        self.safety_margin_seconds = safety_margin_seconds
        self.salvage = salvage

        # State kept on the object so another thread (the watchdog) can
        # build a result from whatever has happened so far.
        self.steps: List[StepMetrics] = []
        self._task_id = "unknown"
        self._benchmark = ""
        self._start = time.perf_counter()

    def build_result(self, success: bool, solution: str, error=None) -> SolutionOutput:
        steps = list(self.steps)
        return SolutionOutput(
            task_id=self._task_id,
            benchmark=self._benchmark,
            success=success,
            solution=solution,
            iterations=len(steps),
            total_requests=sum(1 + s.retries for s in steps),
            total_input_tokens=sum(s.input_tokens for s in steps),
            total_output_tokens=sum(s.output_tokens for s in steps),
            total_time_seconds=time.perf_counter() - self._start,
            steps=steps,
            system_prompt=self.system_prompt,
            error=error,
        )

    def _timeout_result(self, detail: str = "") -> SolutionOutput:
        solution = ""
        if self.salvage is not None:
            # Use what is left before the hard limit (at most 20s), keeping
            # 3s to write the result file.
            hard_end = self._start + self.timeout_seconds - 3
            budget = min(20.0, hard_end - time.perf_counter())
            if budget > 1:
                solution = run_with_timeout(self.salvage, budget) or ""
        message = f"Reached timeout ({self.timeout_seconds}s) before completing"
        if detail:
            message += f": {detail}"
        return self.build_result(False, solution, message)

    def run(self, task_id: str, benchmark: str, user_task: str) -> SolutionOutput:
        self._task_id, self._benchmark = task_id, benchmark
        self._start = self.start_time if self.start_time is not None else time.perf_counter()
        soft_deadline = self._start + self.timeout_seconds - self.safety_margin_seconds

        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user_task},
        ]
        self.steps.clear()
        steps = self.steps

        total_input_tokens = 0
        total_output_tokens = 0
        # code -> (step, observation) of its last run, to catch an LLM that
        # repeats the same call hoping for a different answer.
        previous_runs = {}

        for step in range(1, self.max_iterations + 1):
            if time.perf_counter() >= soft_deadline:
                return self._timeout_result()

            try:
                response = self.llm_provider.generate(
                    messages, stop_sequences=STOP_SEQUENCES, deadline=soft_deadline
                )
            except Exception as e:
                out_of_time = isinstance(e, TimeoutError) or time.perf_counter() >= soft_deadline - 1
                if out_of_time:
                    return self._timeout_result(str(e))
                return self.build_result(False, "", f"LLM request failed at step {step}: {e}")

            total_input_tokens += response.input_tokens
            total_output_tokens += response.output_tokens

            extraction = extract_python_code_block(response.text)
            if extraction.code is None:
                # The warning already says what to do (and why nothing was run).
                observation = extraction.warning or (
                    "No code block found. Reply with 'Thought: ...' followed by a "
                    "```python ... ``` block, then <end_code>."
                )
            else:
                observation = self.sandbox.execute(extraction.code) or "(no output, use print())"
                if extraction.warning:
                    # Tell the LLM what was interpreted, so it is never left guessing.
                    observation = f"[Note] {extraction.warning}\n{observation}"
                previous = previous_runs.get(extraction.code)
                previous_runs[extraction.code] = (step, observation)
                if previous is not None and previous[1] == observation:
                    # Weak models can loop on one call until the budget runs out.
                    observation = (
                        f"[Note] You already ran exactly this code at step {previous[0]} "
                        "and got the same result. Running it again will not change it: "
                        "try a different approach.\n" + observation
                    )

            steps.append(
                StepMetrics(
                    step=step,
                    input_tokens=response.input_tokens,
                    output_tokens=response.output_tokens,
                    request_time_ms=response.request_time_ms,
                    api_url=self.llm_provider.base_url,
                    model_name=self.llm_provider.model_name,
                    llm_output=response.text,
                    sandbox_input=extraction.code or "",
                    sandbox_output=observation,
                    retries=response.retries,
                )
            )

            if self.sandbox.final_answer_called:
                # A successful final_answer on this very step counts as success even
                # if this step's tokens happen to push the running total over budget:
                # the totals were only knowable after the response that solved the task.
                return self.build_result(True, self.sandbox.final_answer_value)

            if total_input_tokens > self.max_input_tokens:
                return self.build_result(False, "", f"Exceeded max input tokens ({self.max_input_tokens})")
            if total_output_tokens > self.max_output_tokens:
                return self.build_result(False, "", f"Exceeded max output tokens ({self.max_output_tokens})")

            # Some providers (e.g. Cohere) reject empty messages. A reasoning model
            # can spend its whole output budget thinking and return no text at all,
            # so never send an empty assistant message back.
            messages.append({"role": "assistant", "content": response.text or "(empty response)"})
            messages.append({"role": "user", "content": f"Observation:\n{observation}"})

        return self.build_result(
            False, "", f"Reached max iterations ({self.max_iterations}) without final_answer"
        )


def build_system_prompt(sandbox_manual: str, benchmark: str) -> str:
    """Build the system prompt: format rules, available tools, and
    benchmark-specific instructions with one worked example."""
    tools = sandbox_manual.strip() or "(no extra tools connected)"

    if benchmark == "mbpp":
        task_instructions = """For mbpp tasks:
1. Write the requested function as a source-code string. Keep the exact
   function name and parameters from the given signature.
2. Check it with the official tests: print(run_tests(code=solution))
3. If a test fails, fix the code and run the tests again.
4. When all tests pass, submit the source string with final_answer(solution).
Do not hard-code the expected outputs of the tests: implement the general
logic described in the task, since hidden tests are also used for grading.

Example
-------
Task: Write a function to find the square of a number.
Function signature: def square(n):
Tests:
assert square(3) == 9

Thought: I write the function and check it with the official tests.
```python
solution = '''def square(n):
    return n * n
'''
print(run_tests(code=solution))
```
<end_code>
Observation:
{"success": true, "output": "test 1: PASS  (assert square(3) == 9)\\n[run_tests] 1/1 tests passed"}

Thought: All tests pass, I submit the solution.
```python
final_answer(solution)
```
<end_code>
"""
    elif benchmark == "swebench":
        task_instructions = """For swebench tasks:
IMPORTANT: this code runs on your host machine, not inside the task's
container. The target repository (e.g. sympy) is not installed here and
cannot be imported or run directly -- `import sympy` (or any target-repo
package) will fail even if it were on the allowlist, because the package
simply isn't there. Never write code that imports or executes the repo's
own code to "try it out" yourself. Everything that touches the actual
repository -- reading files, searching code, running commands, running
tests -- must go through the tools above; they are the only thing
connected to the container.
1. Read the issue below, then explore the repo with the tools above
   (list_files / search_code / search_function_or_class_definition_in_code /
   find_references / read_file) to find the code causing it.
2. Make the fix with edit_file. old_str must match the current file content
   exactly and uniquely -- read the file again if a call fails or matches
   more than once.
3. Verify with run_tests(). If it fails, keep editing and re-running
   run_tests() until it passes, or you understand why it still can't.
4. When done, call get_patch() to get the diff of your changes, then submit
   it as-is with final_answer(patch). Never write the diff text yourself --
   only submit exactly what get_patch() returned.
Explore before you edit: read enough of the surrounding code to understand
what you're changing, and only touch what's needed to fix the issue.

Example
-------
Issue: Vector.__add__ raises when adding the zero vector.

Thought: I look for the Vector class definition first.
```python
print(search_function_or_class_definition_in_code(name="Vector"))
```
<end_code>
Observation:
/testbed/sympy/physics/vector/vector.py:20 class Vector:

Thought: I read the __add__ method in that area.
```python
print(read_file(filepath="/testbed/sympy/physics/vector/vector.py", start_line=1, end_line=60))
```
<end_code>
Observation:
   ...
   45      def __add__(self, other):
   46          return Vector(self.args + other.args)
   ...

Thought: I fix it to special-case zero, then re-run the tests.
```python
print(edit_file(
    filepath="/testbed/sympy/physics/vector/vector.py",
    old_str="    def __add__(self, other):\n        return Vector(self.args + other.args)",
    new_str="    def __add__(self, other):\n        if other == 0:\n            return self\n        return Vector(self.args + other.args)",
))
print(run_tests())
```
<end_code>
Observation:
[run_tests] tests passed

Thought: Tests pass, I get the diff and submit it.
```python
patch = get_patch()
final_answer(patch)
```
<end_code>
"""
    else:
        task_instructions = f"For {benchmark} tasks: use the tools above to solve the task, then submit with final_answer."

    return f"""You are a coding agent that solves tasks by writing and running Python code.

At each turn, write:
Thought: one or two short sentences about what to do next
```python
# code to run
```
<end_code>

Rules:
- Your code is executed and its printed output is sent back to you as "Observation:".
  Only what you print() is visible, so print the results you need.
- Variables and functions you define persist between turns.
- Never write the Observation yourself; stop after <end_code>.
- Keep your answers short: your total output is limited.
- exec(), eval() and compile() are not available.
- Always call tools with keyword arguments, e.g. run_tests(code=solution).
- When you are done, call final_answer(answer) inside a code block.

Always available:
- final_answer(answer: str) -> None : submit your final answer and end the task.

Other tools:
{tools}

{task_instructions}"""