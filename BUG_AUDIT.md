# California — Bug Audit

**Date:** 2026-07-12
**Scope:** Whole codebase — `core/`, `services/`, `main.py`, `config.yaml`, `hardware/`, `tools/`.
**Type:** Identification only. No code was changed.

**Baseline (pre-audit):**
- `python -m py_compile` on all production modules **passes** — *except* the stale
  dead-code file `services/tts copy.py`, which has a syntax error (see L1). Excluding the
  two `* copy.py` files, compile is clean.
- `python -m unittest discover -s tests -v` → **63 tests, all passing.**

So every finding below is a *latent* issue in code that currently compiles and passes its
(partial) test suite — not a regression.

**Method note:** These findings were each re-read against the real source and tagged with a
confidence level. A first automated pass produced ~23 candidates; roughly a third did not
survive verification and are listed in the *Rejected* appendix so they are not
re-investigated later.

---

## Summary table

| ID | Title | Location | Severity | Confidence |
|----|-------|----------|----------|------------|
| H1 | Barge-in / "stop" command is non-functional (`_interrupted` never set) | `core/orchestrator.py:363,540,565,590,622,672` | High | Confirmed |
| H2 | Claude provider does not actually stream tokens | `services/llm.py:270,279-282` | High | Confirmed |
| H3 | Default TTS provider `kokoro` missing from `requirements.txt` | `config.yaml:133` + `requirements.txt` | High | Confirmed |
| M1 | `_adb` uses `shell=True`; timeout may orphan hung `adb.exe` on Windows | `services/media_service.py:47-55` | Medium | Likely |
| M2 | Multi-`tool_use`-block responses build malformed Claude message history | `services/llm.py:283-297` | Medium | Likely |
| M3 | Synchronous tool dispatch blocks the token/TTS stream | `core/orchestrator.py:536-550` | Medium | Confirmed (by-design limit) |
| M4 | LLM tool-loop `messages` list never trimmed during multi-tool turns | `services/llm.py:267-301,398-402` | Medium | Confirmed |
| L1 | Dead-code files, one with a syntax error | `services/tts copy.py`, `services/sentence_chunker copy.py` | Low | Confirmed |
| L2 | Dead `system_prompt_` config key contains offensive text | `config.yaml:72-85` | Low | Confirmed |
| L3 | Hardcoded absolute Windows paths, non-portable to Pi | `config.yaml:135`, `media.adb_path` | Low | Confirmed |
| L4 | Sentence chunker silently drops chunks that sanitize to empty | `services/sentence_chunker.py:71-82` | Low | Confirmed |
| L5 | Edge TTS creates/closes a fresh asyncio loop per call | `services/tts.py:120-124` | Low | Likely |
| L6 | TMDB HTTP errors collapse into a generic "not found" spoken line | `services/stremio_service.py:415-459` | Low | Confirmed |
| L7 | `lang_code` comment typo (`"a"` labelled both British and American) | `config.yaml:147` | Low | Confirmed |
| L8 | `match/case` needs Python ≥3.10 but no minimum version documented | `hardware/led_controller.py` | Low | Likely |
| L9 | `SurfsharkService` constructed even when VPN routing disabled | `core/orchestrator.py:338` | Low | Confirmed |

---

## High

### H1 — Barge-in / "stop" command is non-functional
**Location:** `core/orchestrator.py` — flag declared `:363`, checked `:540`, `:590`, `:622`, reset `:565`; "stop" handler `:672-674`.
**What's wrong:** `self._interrupted` is only ever assigned `False` (`:363`, `:565`). Grep across the whole repo finds **no** `_interrupted = True` anywhere. So the three barge-in guards (`if self._interrupted:` in `_stream_response`, `_tts_worker`, `_audio_player_worker`) never fire — they are dead code. Separately, the "stop" / "shut up" voice command calls `self.audio.stop_playback()` (`:673` → `sd.stop()`), which halts only the sentence currently playing; because it does not set `_interrupted`, the TTS worker and audio-player threads keep pulling queued sentences and playback resumes with the next one.
**Trigger:** Say "stop" / "shut up" mid-response, or speak over California expecting barge-in.
**Impact:** The advertised interrupt/barge-in behavior does not work; "stop" only skips one sentence.
**Suggested fix:** Set `self._interrupted = True` in `_handle_command` for the stop phrases (and wherever barge-in should trigger) before calling `stop_playback()`; consider a `threading.Event` instead of a bare bool for clean cross-thread signaling, and drain both queues on interrupt.

### H2 — Claude provider does not actually stream tokens
**Location:** `services/llm.py:270` (`self.client.messages.create(...)` with no `stream=True`), yield at `:279-282`.
**What's wrong:** The Claude path calls the non-streaming `messages.create()` and then iterates the completed `response.content` blocks, yielding each block's full text at once. There is no token-level streaming. The OpenAI-compatible path, by contrast, uses `stream=True` (`:320`) and yields real deltas. Claude is the configured default provider (`config.yaml:49`).
**Trigger:** Any normal turn while `llm.provider: "claude"`.
**Impact:** The user waits for the *entire* LLM generation (up to `max_tokens: 1000`) before hearing the first word — directly defeating the "first sentence while the LLM is still generating" design the project calls non-negotiable. Sentence-chunker/TTS overlap provides no latency benefit on the default provider.
**Suggested fix:** Switch `_stream_claude` to `self.client.messages.stream(...)` and yield `text` deltas from the streaming events (handling `tool_use` accumulation as the OpenAI path does).

### H3 — Default TTS provider `kokoro` is missing from `requirements.txt`
**Location:** `config.yaml:133` (`provider: "kokoro"`); `requirements.txt` (no `kokoro` entry); init at `services/tts.py:189-201`.
**What's wrong:** The shipped default TTS provider is `kokoro`, but `kokoro` appears nowhere in `requirements.txt`. On a clean `pip install -r requirements.txt`, `_init_kokoro` raises `RuntimeError("kokoro package not found …")` during `TTSService.__init__`, which is constructed unconditionally in `Orchestrator.__init__` (`orchestrator.py:330`) — so the whole app fails to start.
**Trigger:** Fresh environment following the documented install, default config.
**Impact:** App will not boot out of the box.
**Suggested fix:** Add a pinned `kokoro` (and any runtime deps it needs) to `requirements.txt`, or change the default provider to one that is installed (`edge`).

---

## Medium

### M1 — `_adb` uses `shell=True`; timeout may orphan a hung `adb.exe` on Windows
**Location:** `services/media_service.py:47-55` (primary `_adb`); contrast `_adb_exec:88-96` (list form, `shell=False`).
**What's wrong:** `_adb` builds a single command string and runs `subprocess.run(cmd, shell=True, timeout=…)`. On Windows, `shell=True` spawns `cmd.exe` which spawns `adb.exe`; on `TimeoutExpired`, Python terminates the shell but the grandchild `adb.exe` can survive, so a genuinely hung ADB call is not reliably killed. The sibling `_adb_exec` already uses the safer list form — the two ADB entry points are inconsistent.
**Trigger:** TV/ADB hang (Wi-Fi drop, sleeping Mi Box) during a shell-form ADB call.
**Impact:** Leaked adb processes and possible repeated stalls; undercuts the timeout guarantees the rest of the code relies on.
**Suggested fix:** Route `_adb` through the same list-based, `shell=False` execution as `_adb_exec`; drop `shell=True`.

### M2 — Multiple `tool_use` blocks in one Claude response build malformed history
**Location:** `services/llm.py:283-297`.
**What's wrong:** The assistant message append (`messages.append({"role":"assistant","content":response.content})`) and the `tool_result` user append happen **inside** the `for block in response.content` loop. If Claude emits two or more `tool_use` blocks in a single response, the full assistant content is appended once per block and multiple separate `tool_result` user messages are produced. Anthropic expects exactly one assistant message followed by one user message containing all `tool_result`s — so the next loop iteration (`:270`) can send a malformed sequence and error.
**Trigger:** Claude returns 2+ tool calls in one turn (possible for compound requests).
**Impact:** Occasional API error / broken turn after multi-tool responses.
**Suggested fix:** Move the assistant-message append out of the per-block loop; collect all `tool_result`s into a single user message appended once after processing every block.

### M3 — Synchronous tool dispatch blocks the token/TTS stream
**Location:** `core/orchestrator.py:536-550` (stream loop) → `llm` `tool_handler` → `_dispatch_tv` → ADB / Stremio UI scans.
**What's wrong:** Tool calls run synchronously inside the generator that the sentence-chunker/TTS loop consumes. A `control_tv` call can spend up to `media.adb_timeout_ms` (~15s) per ADB command, and Stremio provider scanning issues repeated UI dumps (`media_service.dump_ui_hierarchy`, `ui_dump_timeout_ms × ui_dump_retry_count`), all before the generator yields further text.
**Trigger:** Any media/TV request, especially Stremio playback that scans providers.
**Impact:** Multi-second gap before any audio on tool-driven turns. Bounded (not a hang), but at odds with the latency target — and compounded by H2 on the Claude default.
**Suggested fix:** Consider dispatching tool calls on a bounded worker with an overall deadline, emitting a short spoken acknowledgement immediately while the tool completes.

### M4 — LLM tool-loop `messages` list never trimmed during multi-tool turns
**Location:** `services/llm.py:267-301` and `:303-396`; trimming only at `:398-402` on `self.history`.
**What's wrong:** Inside both provider tool loops, the local `messages` list grows with each assistant/tool round-trip and is never trimmed within the loop. Only `self.history` (final user/assistant text) is trimmed, and only via `stream_response` before the loop. A turn with several tool calls can push the request past the model's context/token budget.
**Trigger:** A single turn that chains many tool calls.
**Impact:** Possible token-limit error or cost spike on tool-heavy turns.
**Suggested fix:** Cap or summarize `messages` inside the loop once it exceeds a threshold, preserving the required assistant/tool pairing.

---

## Low

- **L1 — Dead-code files.** `services/tts copy.py` (contains an `IndentationError`, so a naive
  `py_compile services/*.py` fails) and `services/sentence_chunker copy.py` are stale
  duplicates. They are not imported anywhere. *Fix:* delete both.
- **L2 — Dead `system_prompt_` key with offensive content.** `config.yaml:72` defines
  `system_prompt_` (trailing underscore); the code reads `system_prompt` (`services/llm.py:136`),
  so this block is never used. Its body (`:83-85`) contains overtly racist text. It is inert but
  a real reputational/safety liability sitting in the repo. *Fix:* delete the entire
  `system_prompt_` block.
- **L3 — Hardcoded absolute Windows paths.** `config.yaml:135` pins the Piper model to
  `C:\Users\Migue\Downloads\california-project\california\models\…` (a path that doesn't even
  match this checkout, `california-project - codex`), and `media.adb_path` is Windows-specific.
  Non-portable to the Raspberry Pi target. *Fix:* use relative/config-relative paths and a
  platform-appropriate `adb` default.
- **L4 — Chunks that sanitize to empty are silently dropped.** `services/sentence_chunker.py:71-74`
  and `:78-82` only `yield` when `clean` is truthy. If a whole response sanitizes to empty
  (e.g. `"---"`), nothing is spoken and there is no fallback/log. Edge case. *Fix:* log a warning
  and/or fall back to the unsanitized text when the entire response yields nothing.
- **L5 — Edge TTS asyncio loop churn.** `services/tts.py:120-124` creates and closes a new event
  loop per call without `asyncio.set_event_loop`. Wasteful and can surface "Event loop is closed"
  noise from aiohttp on Windows. Edge is not the default. *Fix:* use `asyncio.run(...)` or a reused
  loop.
- **L6 — TMDB HTTP errors collapse into a generic line.** `services/stremio_service.py:415-459`
  lets `raise_for_status()` (429/timeout/5xx) propagate; it is caught at
  `core/orchestrator.py:236-238` and spoken as "I couldn't find {title} in Stremio or TMDB." No
  crash, but rate-limit/outage is indistinguishable from a genuine miss. *Fix:* catch and classify
  HTTP errors in `_tmdb_get`, log specifics, and optionally retry on 429.
- **L7 — Comment typo.** `config.yaml:147`: `"a" = British English, "a" = American English` — both
  say `"a"`. Per Kokoro / CLAUDE.md, `"a"` is American and `"b"` British. Cosmetic. *Fix:* correct
  the comment.
- **L8 — Undocumented Python minimum.** `hardware/led_controller.py` uses `match/case` (Python
  ≥3.10); neither `requirements.txt` nor setup states a minimum. *Fix:* document the minimum Python
  version.
- **L9 — Surfshark constructed while disabled.** `core/orchestrator.py:338` builds
  `SurfsharkService` whenever `media.enabled` is true, even though `vpn_routing_enabled` is `false`
  and `ensure_route()` is never called. Harmless init waste. *Fix:* gate construction on
  `vpn_routing_enabled`.

---

## Rejected candidates (verified NON-issues — do not re-investigate)

These were flagged by the initial automated pass but did not survive reading the real code:

- **`llm.claude.web_search` "unused."** It **is** used: `services/llm.py:157` reads it and
  `:253-256` adds the web-search tool. False positive.
- **STT tempfile "file-descriptor leak."** The `tmp.read()` happens **inside** the
  `with tempfile.NamedTemporaryFile(...)` block (`services/stt.py:55-65`); the context manager
  closes it. No leak.
- **MediaService cooldown "never reset."** The stale `_last_fail_time` is guarded by
  `not self._connected` (`media_service.py:126`); any real failure re-stamps it via `connect()`
  (`:118`). Non-issue.
- **`_is_decimal` "only single digit / splits 3.14159."** It checks one digit on each side of the
  period (`sentence_chunker.py:210-213`); for `3.14159` it returns `True`, so no split. Works
  correctly.
- **Wake-word `_ppn_buffer` "not thread-safe."** `process_audio` is only called from the single
  main-thread idle loop (`orchestrator.py:431`). No concurrency. Non-issue.
- **VAD `_recording_start`/`_silence_start` "unsynchronized."** Only touched by the single
  recording thread (`orchestrator.py:484,492`). Non-issue.
- **`tts_queue.empty()` then `get_nowait()` race.** Guarded by `except queue.Empty: break`, single
  consumer per queue (`orchestrator.py:591-595,624-628`). Non-issue.
- **Stremio "double authentication on startup."** `sync_library` only re-authenticates when
  `_auth_key` is `None` (`stremio_service.py:180-181`); this is correct lazy-retry, not redundant
  auth. Non-issue.
