# Prefix cache diagnostic

This script sends synthetic text only. It does not read the story database or app
credentials. The default is a dry run; `--run` authorizes at most six sequential
requests, capped at 64 output tokens each, with no automatic retries. A request
failure stops the run with a nonzero exit code.

```powershell
.venv/Scripts/python.exe tools/probe_prefix_cache.py --base-url https://example.com
.venv/Scripts/python.exe tools/probe_prefix_cache.py --base-url https://example.com --mode native --run
.venv/Scripts/python.exe tools/probe_prefix_cache.py --base-url https://example.com --api responses --mode default --rule-count 400 --run
.venv/Scripts/python.exe tools/probe_prefix_cache.py --base-url https://example.com --api messages --mode native --rule-count 400 --run
```

Enter the test key at the hidden prompt, or supply `TAVERN_PROBE_API_KEY` through
the process environment. Do not put keys in command arguments or tracked files.
Requests connect directly by default. Use `--proxy <HTTP(S) URL>` to select an explicit proxy; `--proxy ""` uses a direct connection.

Each run stores its synthetic requests, SHA-256 hashes, timestamps, HTTP status,
raw usage, and normalized cache metrics under `.cache/prefix-cache-probes/`.
Missing counters remain unknown; aliases are not added together. Conflicting
write counters are flagged. Authorization headers and response prose are not saved.

The six cases are:

1. `baseline`: fixed reference, history, current user A, trailing system status 01.
2. `exact_repeat`: exactly the same request.
3. `user_tail_changed`: only current user A changes to B.
4. `system_tail_changed`: only trailing status 01 changes to 02, relative to case 3.
5. `changed_exact_repeat`: exactly repeats case 4.
6. `append_exchange`: preserves case 4 and appends assistant OK plus a new user input.

Use `--status-role user` to represent the trailing test status as a user message.
The case name `system_tail_changed` still means the trailing status changes; it
does not change that message's role. This is a synthetic comparison, not a proposal
to demote application authorization instructions.

Cache modes are separate protocol experiments:

- `explicit`: current app no-write payload, `prompt_cache_options.mode=explicit`, no marker.
- `instructions`: the same option with the app's `prompt_cache_breakpoint` marker.
  This is an OpenAI explicit-cache field for supported newer OpenAI models, not
  a guarantee that a Claude relay will translate it.
- `native`: `cache_control.type=ephemeral` on the leading reference block, no custom option.
- `default`: no explicit cache options or markers.

`--api responses` sends the equivalent ordered input with `store=false`,
`max_output_tokens`, and `input_text` blocks. `--cache-key` can supply a fixed
OpenAI cache routing key; it is not a breakpoint. `--api messages` tests the
Anthropic Messages envelope directly with `x-api-key` and `anthropic-version`.
It moves system messages into ordered top-level system blocks, keeping the
stable marked block separate from the dynamic status. Messages probes permit
only default/native modes and no OpenAI routing key. Native `input_tokens`
excludes cache read/write tokens; add those fields for a total input count.

For OpenAI explicit-cache compatibility, also test `--rule-role developer`.
Some compatible routes may hoist system messages to top-level instructions and
lose a content-block marker. A 200 response does not establish marker support;
compare usage and preserve any explicit unsupported-parameter errors. Never
treat Anthropic `cache_control` as a universal OpenAI/Gemini parameter.

`--verify-tail` replaces the constant OK task with English-to-French color
translation. Changing the user query must change `rouge` to `bleu`; changing
the final system instruction to uppercase must produce `BLEU`; the appended
query must produce `VERT`. Synthetic answers and exact-match checks are saved.
A failed check is not by itself proof of a dropped field or stale cache: compare
fresh cold requests and unmarked requests to distinguish model compliance issues.

Use a different `--tag` for each independent group to avoid warming another
experiment's cache. Reusing a tag intentionally tests cross-mode cache reuse and
must not be interpreted as proof that a newly added marker works. Runs also have
unique output directories and refuse to overwrite an existing directory.

The default `--rule-count 240` gives a fixed leading block of roughly 5.8k tokens
under the local reference tokenizer, above Opus 4.6's documented 4,096-token
minimum. Actual provider counts differ. `--rule-count 110` reproduces the shorter
initial diagnostic fixtures, whose marked prefix was only about 2.7k tokens.
Never infer marker incompatibility solely from those shorter fixtures.
Use `--rule-count 400` for roughly 12k total input tokens, with about 9.6k in the
stable leading reference. These are estimates; always inspect returned usage.

The script intentionally excludes tools to isolate message caching. It cannot
observe a relay's transformed upstream request, account routing, or billing page.
Results describe the tested endpoint/model path, not all providers or models.
