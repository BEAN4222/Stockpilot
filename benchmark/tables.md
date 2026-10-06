## Results

| Run | Model | Task | Pass/Fail | Iterations | Input tokens | Output tokens | Wall-clock (s) |
|---|---|---|---|---|---|---|---|
| gemini-3.6-flash | gemini-3.6-flash | django__django-11066 | PASS | 9 | 42071 | 564 | 129.8 |
| gemini-3.6-flash | gemini-3.6-flash | pydata__xarray-4629 | PASS | 10 | 65893 | 451 | 72.0 |
| gemini-3.6-flash | gemini-3.6-flash | sympy__sympy-18189 | FAIL | 3 | 13011 | 2213 | 52.4 |
| laguna-s-2.1 | poolside/laguna-s-2.1:free | django__django-11066 | PASS | 6 | 26800 | 498 | 38.8 |
| laguna-s-2.1 | poolside/laguna-s-2.1:free | pydata__xarray-4629 | FAIL | 21 | 78710 | 1606 | 84.8 |
| laguna-s-2.1 | poolside/laguna-s-2.1:free | sympy__sympy-18189 | PASS | 1 | 2711 | 242 | 35.7 |
| nemotron-3-ultra | nvidia/nemotron-3-ultra-550b-a55b:free | django__django-11066 | FAIL | 30 | 137129 | 959 | 82.5 |
| nemotron-3-ultra | nvidia/nemotron-3-ultra-550b-a55b:free | pydata__xarray-4629 | PASS | 15 | 58480 | 1919 | 117.1 |
| nemotron-3-ultra | nvidia/nemotron-3-ultra-550b-a55b:free | sympy__sympy-18189 | PASS | 13 | 44401 | 946 | 114.7 |
| north-mini-code | cohere/north-mini-code:free | django__django-11066 | PASS | 7 | 22907 | 420 | 18.4 |
| north-mini-code | cohere/north-mini-code:free | pydata__xarray-4629 | FAIL | 30 | 234000 | 1511 | 38.1 |
| north-mini-code | cohere/north-mini-code:free | sympy__sympy-18189 | FAIL | 20 | 151020 | 1796 | 46.6 |
| qwen3.8-27b | qwen/qwen3.8-27b:free | django__django-11066 | PASS | 4 | 13604 | 198 | 39.1 |
| qwen3.8-27b | qwen/qwen3.8-27b:free | pydata__xarray-4629 | PASS | 4 | 13349 | 212 | 45.2 |
| qwen3.8-27b | qwen/qwen3.8-27b:free | sympy__sympy-18189 | PASS | 4 | 13081 | 237 | 53.3 |
| qwen3.8-27b-no-example | qwen/qwen3.8-27b:free | django__django-11066 | FAIL | 30 | 93463 | 770 | 51.9 |
| qwen3.8-27b-no-example | qwen/qwen3.8-27b:free | pydata__xarray-4629 | FAIL | 4 | 10138 | 202 | 17.8 |
| qwen3.8-27b-no-example | ? | sympy__sympy-18189 | FAIL | 0 | 0 | 0 | 13.5 |

## Provider reliability

| Run | API | Avg response (ms) | Requests | Retries | Runs without provider failure |
|---|---|---|---|---|---|
| gemini-3.6-flash | https://generativelanguage.googleapis.com/v1beta/openai | 7060 | 24 | 2 | 2/3 |
| laguna-s-2.1 | https://openrouter.ai/api/v1 | 3929 | 32 | 4 | 2/3 |
| nemotron-3-ultra | https://openrouter.ai/api/v1 | 4392 | 58 | 0 | 3/3 |
| north-mini-code | https://openrouter.ai/api/v1 | 913 | 57 | 0 | 2/3 |
| qwen3.8-27b | https://openrouter.ai/api/v1 | 2983 | 12 | 0 | 3/3 |
| qwen3.8-27b-no-example | https://openrouter.ai/api/v1 | 1345 | 37 | 3 | 1/3 |

## Intermediary metrics

| Run | Task | First step touching a patched file | First step tests pass | Extra steps before final_answer |
|---|---|---|---|---|
| gemini-3.6-flash | django__django-11066 | 3 | 7 | 1 |
| gemini-3.6-flash | pydata__xarray-4629 | 3 | 8 | 1 |
| gemini-3.6-flash | sympy__sympy-18189 | - | - | - |
| laguna-s-2.1 | django__django-11066 | 1 | 5 | 0 |
| laguna-s-2.1 | pydata__xarray-4629 | - | - | - |
| laguna-s-2.1 | sympy__sympy-18189 | 1 | 1 | 0 |
| nemotron-3-ultra | django__django-11066 | - | 30 | - |
| nemotron-3-ultra | pydata__xarray-4629 | 2 | 12 | 2 |
| nemotron-3-ultra | sympy__sympy-18189 | 1 | 8 | 4 |
| north-mini-code | django__django-11066 | 3 | 6 | 0 |
| north-mini-code | pydata__xarray-4629 | - | 7 | - |
| north-mini-code | sympy__sympy-18189 | - | - | - |
| qwen3.8-27b | django__django-11066 | 1 | 3 | 0 |
| qwen3.8-27b | pydata__xarray-4629 | 1 | 3 | 0 |
| qwen3.8-27b | sympy__sympy-18189 | 1 | 3 | 0 |
| qwen3.8-27b-no-example | django__django-11066 | - | - | - |
| qwen3.8-27b-no-example | pydata__xarray-4629 | - | - | - |
| qwen3.8-27b-no-example | sympy__sympy-18189 | - | - | - |

## Errors

- gemini-3.6-flash / sympy__sympy-18189: LLM request failed at step 4: HTTP 429 from https://generativelanguage.googleapis.com/v1beta/openai/chat/completions: [{
  "error": {
    "code": 429,
- laguna-s-2.1 / pydata__xarray-4629: LLM request failed at step 22: HTTP 429 from https://openrouter.ai/api/v1/chat/completions: {"error":{"message":"Provider returned error","code":429,"
- nemotron-3-ultra / django__django-11066: Reached max iterations (30) without final_answer
- north-mini-code / pydata__xarray-4629: Reached max iterations (30) without final_answer
- north-mini-code / sympy__sympy-18189: LLM request failed at step 21: HTTP 429 from https://openrouter.ai/api/v1/chat/completions: {"error":{"message":"Rate limit exceeded: free-models-per-
- qwen3.8-27b-no-example / django__django-11066: Reached max iterations (30) without final_answer
- qwen3.8-27b-no-example / pydata__xarray-4629: LLM request failed at step 5: HTTP 429 from https://openrouter.ai/api/v1/chat/completions: {"error":{"message":"Rate limit exceeded: free-models-per-d
- qwen3.8-27b-no-example / sympy__sympy-18189: LLM request failed at step 1: HTTP 429 from https://openrouter.ai/api/v1/chat/completions: {"error":{"message":"Rate limit exceeded: free-models-per-d
