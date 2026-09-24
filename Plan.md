# Voice Browser Agent

> Speak a goal. Watch your own Chrome do it — in about a second.

A **local, single-user, voice-input browser agent** built on top of
[`browser-use/jev-ultrafast`](https://github.com/browser-use/jev-ultrafast).
You hold a hotkey, speak a command, release, and the agent drives your
real Chrome on your laptop. Voice is **input only** — there is **no TTS**.
All feedback is shown in the browser itself and in a small local status page.

This README is the **complete build brief**. It is written so that a coding
agent (Claude Code) can build the whole system from it without further
context. Read it fully before writing code.

---

## Table of contents

1. [Goals and non-goals](#1-goals-and-non-goals)
2. [Background: how jev-ultrafast works](#2-background-how-jev-ultrafast-works)
3. [System architecture](#3-system-architecture)
4. [Latency design (the core of this project)](#4-latency-design-the-core-of-this-project)
5. [Tech stack](#5-tech-stack)
6. [Repository layout](#6-repository-layout)
7. [Component specifications](#7-component-specifications)
8. [Session state machine](#8-session-state-machine)
9. [Event types](#9-event-types)
10. [Jev question catalogue](#10-jev-question-catalogue)
11. [Safety rules (non-negotiable)](#11-safety-rules-non-negotiable)
12. [Logging and tracing](#12-logging-and-tracing)
13. [Evaluation harness](#13-evaluation-harness)
14. [Milestones and exit criteria](#14-milestones-and-exit-criteria)
15. [Configuration](#15-configuration)
16. [Setup on the laptop](#16-setup-on-the-laptop)
17. [Instructions for Claude Code](#17-instructions-for-claude-code)
18. [Known limitations and things to verify](#18-known-limitations-and-things-to-verify)
19. [References](#19-references)

---

## 1. Goals and non-goals

### Goals

- **Voice in, browser action out.** Push-to-talk → speech-to-text → the agent
  operates the user's own Chrome.
- **Ultra-low latency.** Target **p50 ≤ 1.0 s** from hotkey release to the
  first browser action, **p95 ≤ 2.0 s**. Every design choice below serves this.
- **Dynamic start — no URL needed.** The user never pastes a URL. The agent
  works on the tab that is already open, opens a site the user names
  ("open ixigo and…"), picks a known site for the task, or falls back to web
  search — and can move between sites mid-task.
- **Mid-task corrections by voice.** "No, make it Saturday" updates the running
  task without restarting it.
- **Ask, don't guess.** If a required value is missing, the agent pauses and
  asks on screen; the user answers by voice.
- **Safe on real accounts.** Irreversible actions always require a spoken
  "yes". The agent never types passwords, OTPs, CVVs, or card numbers.
- **Good with Indian-accented English.** Indian names, cities, dates, and rupee
  amounts, spoken in English. (AssemblyAI Universal Streaming does not support
  Tamil; see §18 for Hindi and upgrade options.)

### Non-goals

- No TTS or spoken replies.
- No multi-user support, cloud hosting, Chrome extension, or Web Store release.
- No database server. Logs are JSONL files.
- No site-specific scripts. The policy must stay general (same as jev-ultrafast).

---

## 2. Background: how jev-ultrafast works

Read this section carefully — the agent core is reused almost unchanged.

### 2.1 The idea

jev-ultrafast turns every page into a **numbered menu** and makes a model
**pick from the menu** instead of generating actions.

```
[1] button    Change ticket type · Round trip
[2] combobox  Where from?        · San Francisco
[3] combobox  Where to?          · empty
[4] textbox   Departure          · empty
```

Operations: `CLICK`, `TYPE_TEXT`, `SELECT`, `SCROLL_UP`, `SCROLL_DOWN`, `WAIT`,
`DONE`, `BLOCKED`. Only valid operation/target combinations are offered.

### 2.2 The three actors

| Actor | What it is | Job |
|---|---|---|
| `snapshot.js` | JavaScript injected into the page via CDP | Reads visible interactive controls (role, label, value, state) and visible text in **one atomic browser call**; keeps references to the real DOM nodes; produces a page **fingerprint** |
| **Jev** (TypeSafe) | A hosted **"System One" decision model**. It does not generate text; it returns typed answers with **probabilities and a confidence** in a single pass (~70–500 ms) | Picks the **operation** and the **target** |
| Small LLM (Mercury; the upstream demo reaches it via OpenRouter, this project calls Inception directly) | A normal fast text model | Writes the value to type, **only** when the operation is `TYPE_TEXT` |

Jev still understands language — it reads the goal, labels, values, page text,
and recent actions — but it answers by scoring options, not by writing or
reasoning. Each question must be a small, well-scoped "gut-check" judgement.

### 2.3 Speculative fan-out (why it's fast)

One Jev request asks several questions at once:

- `operation` — which operation?
- `click_target` — if CLICK, which element?
- `type_text_target` — if TYPE_TEXT, which element?
- `select_target` — if SELECT, which element/option?

All answers return in **one network round trip**. The code uses only the target
head that matches the chosen operation and discards the rest. Jev bills input
tokens only; output is free, so extra questions are nearly free.

### 2.4 The loop (`agent.py`)

Each **tick** = `predict` then `act`:

1. **predict** — re-observe if the page is stale; enforce step budget; call
   `choose(page, goal, history)`; store the decision with the page fingerprint.
2. **act** — refuse unless the decision's fingerprint matches the current page;
   **consume the decision before acting** (a retry can never double-click);
   for `TYPE_TEXT`, get text from the small LLM (reuse it on a stale-page retry
   if the input is identical); execute via `browser.act()` (which re-checks
   freshness and occlusion); **log the action before re-observing**; observe the
   new page; record whether it changed.
3. **stuck detection** — three consecutive non-wait actions with no page change
   → `blocked`.

`Agent.run()` is a **generator that yields after every tick** — this is the hook
this project uses to change the goal between ticks.

### 2.5 Files in jev-ultrafast

| File | Job |
|---|---|
| `jev_ultrafast/agent.py` | The loop and text-helper hand-off |
| `jev_ultrafast/snapshot.js` | Atomic DOM snapshot, indexed controls, freshness guards |
| `jev_ultrafast/browser.py` | Browser connection (Browser Harness), geometry, execution |
| `jev_ultrafast/model.py` | `action_space()`, `choose()` (Jev), `field_context()`, `field_text()` (small LLM) |
| `jev_ultrafast/questions.py` | Model instructions, `MAX_STEPS` |
| `jev_ultrafast/demo.py` | Local inspector at `http://127.0.0.1:8766` |

### 2.6 The Jev request shape used by jev-ultrafast

`POST https://api.typesafe.ai/v1/systemone` with `Authorization: Bearer $TYPESAFE_API_KEY`:

```json
{
  "model": "jev-latest",
  "state": {
    "page": { "url": "...", "title": "...", "text": "..." },
    "elements": [ { "index": "1", "role": "button", "label": "...", "operations": ["CLICK"] } ],
    "recent_actions": [ { "action": "...", "kind": "...", "text": "...", "page_changed": true } ]
  },
  "questions": {
    "operation": {
      "type": "choice",
      "criteria": { "CLICK": "Click an element...", "TYPE_TEXT": "...", "DONE": "...", "BLOCKED": "..." },
      "instructions": { "goal": "...", "rules": "..." }
    },
    "click_target": {
      "type": "choice",
      "criteria": { "1": { "element": "[1] ...", "current_value": "..." } },
      "instructions": { "goal": "...", "operation": "CLICK", "rules": ["...", "..."] }
    }
  }
}
```

Response: `result["answers"][name]` → `{ "choice", "probabilities": {id: p}, "confidence" }`,
plus `result["model"]` (the versioned model that answered) and `result["usage"]`.
`validate_choice()` rejects anything malformed (probabilities must sum to ~1,
choice must be the argmax, all ids must exist) — **no action is executed on an
invalid answer**.

Jev also supports boolean questions (`"type": "noul"`) that return the
probability of "yes", and score questions. See TypeSafe docs.

### 2.7 Safety properties inherited (never weaken these)

- Model output **never** becomes selectors, coordinates, shell commands, or JS.
  It only selects an index from a list the code built.
- Every executed target resolves to an **observed DOM node**.
- Freshness is checked before acting and again right before input.
- Covered (occluded) controls are rejected.
- Invalid model output → nothing happens.

### 2.8 Known limits of jev-ultrafast

The demo always **opens its own tab at a URL passed in by the developer**
(`Agent(url, goal)`) and has no operation for going to another site. This
project removes that requirement (§7.11).

Not supported: shadow roots, iframes, canvas, file uploads, pop-up tabs, nested
scrolling, arbitrary keyboard widgets. The DOM reader handles common HTML and
ARIA controls, not the full accessible-name spec. `DONE` is not self-verifying.
Tabs share the existing Chrome profile.

---

## 3. System architecture

Everything runs in **one Python process** on the laptop. Only three things
leave the machine: STT audio (AssemblyAI), Jev calls (TypeSafe), and text-model
calls (Mercury, via Inception's own API).

```
┌──────────────────────────── Laptop ─────────────────────────────┐
│                                                                  │
│  Hotkey (F9) ─► Mic capture ─► STT adapter ═══► AssemblyAI WS    │
│                                    │ final transcript            │
│                                    ▼                             │
│                              Normalizer (rules, <5 ms)           │
│                                    │ utterance                   │
│                                    ▼                             │
│                            Session manager  ◄──── events ────┐   │
│                            (state machine)                   │   │
│                                    │ goal / stop / confirm   │   │
│                                    ▼                         │   │
│              ┌──────────── Agent core (vendored) ──────────┐ │   │
│              │ snapshot.js → Fused Jev request ═══► TypeSafe│ │   │
│              │      ▲            │                         │ │   │
│              │      │            ▼                         │ │   │
│              │      │   Field-value cache ◄═══ Mercury     │ │   │
│              │      │            │         (Inception API) │ │   │
│              │      │            ▼                         │ │   │
│              │      └──── Executor (guards) ───────────────┼─┘   │
│              └──────────────────┬──────────────────────────┘     │
│                                 ▼                                │
│                  Chrome (Browser Harness / CDP)                  │
│                                                                  │
│  Status page http://127.0.0.1:8766 ◄── events (WebSocket)        │
└──────────────────────────────────────────────────────────────────┘
```

### 3.1 Components at a glance

| Component | Responsibility | Uses AI? |
|---|---|---|
| Hotkey + mic | Push-to-talk capture, 16 kHz mono PCM | No |
| STT adapter | Stream audio, return partial + final transcripts | Yes (AssemblyAI) |
| Normalizer | Fix names, resolve dates, numbers, currency | No |
| Session manager | Owns the goal, run state, gates; routes events | No |
| Fused Jev request | Intent + operation + targets in one call | Yes (Jev) |
| Field-value cache | Pre-computes values for visible editable fields | Yes (Mercury, off critical path) |
| Risk scorer | Pre-scores clickable elements for irreversibility | Yes (Jev, off critical path) |
| Snapshot / executor | Read page, act safely | No |
| Status page | Transcript, current step, prompts, timings | No |

### 3.2 One complete turn

1. User holds **F9** and says *"find flights from chennai to bangalore next friday"*.
2. While the key is held, audio streams to AssemblyAI over an **already-open**
   WebSocket; partial transcripts appear on the status page. In parallel the
   agent takes a fresh snapshot if the page fingerprint changed.
3. On release, the adapter sends **`ForceEndpoint`** → AssemblyAI returns the
   final turn (`end_of_turn: true`).
4. Normalizer → *"find flights from Chennai to Bangalore on 2 October 2026"*.
5. Session manager sends **one fused Jev request**: intent question + operation
   + target heads, using the current page and `{current_goal, latest_utterance}`.
6. Jev: intent = `new_task`, operation = `CLICK`, target = `[1] Round trip`.
   The session manager commits the new goal and the executor acts immediately.
7. Off the critical path, Mercury pre-fills the field-value cache for the page's
   editable fields (`From → Chennai`, `To → Bangalore`, `Departure → 2 Oct 2026`),
   and the risk scorer scores the clickable elements.
8. Next ticks run the normal loop. `TYPE_TEXT` reads from the cache (≈0 ms)
   instead of calling the LLM synchronously.
9. Mid-run the user says *"actually Saturday"* → fused request returns
   `correction` → goal updated between ticks → cache invalidated for the date
   field → agent continues.
10. Agent chooses `Search`. The risk scorer marks it safe → no pause. Later a
    `Book now` click would be risky → state `WAITING_CONFIRM`, status page shows
    *"Book this flight for ₹4,200? Say yes or no."*
11. `DONE` → verifier (if defined for the task) → status page shows the result.

### 3.3 Worked example: "open ixigo and book me a flight"

The user has Chrome open on any page, holds F9 and says:
*"Open ixigo and book me a flight from Chennai to Mumbai on 2nd October."*

1. **STT** returns the text. "ixigo" is in `keyterms_prompt`, so it is spelled
   correctly (the gazetteer also maps misspellings like "exigo").
2. **Normalizer** → *"Open ixigo and book a flight from Chennai to Mumbai on
   2026-10-02"*, facts `{site: ixigo, from: Chennai, to: Mumbai, date: 2026-10-02}`.
3. **Start resolver** (§7.11): the user **named a site** that exists in
   `data/sites.yaml`, so the URL comes from the file — no model involved. Fused
   Jev request confirms `intent = new_task`. The agent navigates the **active
   tab** to `https://www.ixigo.com`.
4. **Field-value cache** (background): From → Chennai, To → Mumbai,
   Departure → 2 Oct 2026, plus any passenger fields from `data/profile.yaml`.
5. **Normal loop**: type "Chennai" → click the "Chennai (MAA)" suggestion →
   type "Mumbai" → click "Mumbai (BOM)" → open the calendar → click 2 October →
   click Search.
6. **Results page:** the goal says "book me a flight" but not *which* flight.
   The **choice gate** (§7.11.6) pauses: *"Found 14 flights. Which one? (e.g. the
   cheapest, the 7 AM IndiGo)"*. The user says *"the cheapest"* → `answer`
   intent → appended to the goal → agent clicks it.
7. **Passenger form:** filled from `profile.yaml` via the field cache; anything
   missing is asked by voice.
8. **"Proceed to pay"** is risky → confirm gate: *"Proceed to payment for
   ₹4,850 (IndiGo, 2 Oct, 07:05)? Say yes or no."*
9. **Payment page:** card, CVV, UPI PIN, OTP are **blocked fields**. The agent
   stops; the user completes login/OTP/payment. Run status → `DONE (handed off)`.

Expected: ~10–20 s from command to results page; pauses only for gates.

---

## 4. Latency design (the core of this project)

### 4.1 Latency budget (targets — measure every one)

| Stage | Target p50 | How |
|---|---|---|
| Key release → final transcript | ≤ 300 ms | Warm WebSocket, `ForceEndpoint` on release, small audio frames |
| Normalizer | ≤ 5 ms | Pure rules, precompiled regex, in-memory gazetteer |
| Fused Jev request | ≤ 400 ms | One request, HTTP/2 warm connection, compact state |
| Executor (first action) | ≤ 50 ms | Snapshot already fresh; CDP input |
| **Key release → first action** | **≤ 1.0 s** | Sum of the above, nothing sequential that can be parallel |
| Per subsequent tick | ≤ 600 ms | Jev + act + re-observe, field text from cache |

### 4.2 Optimisations (implement all; each is measurable)

1. **Warm everything at startup.** Open the AssemblyAI streaming WebSocket and keep
   it alive (reconnect on drop). Create one `httpx.AsyncClient(http2=True)` per
   provider and send a cheap warm-up request so TLS + HTTP/2 are established.
   Pre-resolve DNS. Load the gazetteer and compile regexes.
2. **Force the endpoint on key release.** Push-to-talk gives a perfect
   end-of-speech signal. Send `{"type": "ForceEndpoint"}` on release instead of
   waiting for silence-based turn detection, and configure the session so the
   model's own turn detection never ends a turn early while the key is held
   (high end-of-turn confidence threshold, long max turn silence).
3. **Stream small audio frames.** 16 kHz, mono, 16-bit PCM, ~40–100 ms chunks.
   No encoding step (raw PCM or WAV only, as the streaming API requires).
4. **Pre-snapshot while the user is talking.** On key press, check the page
   fingerprint and re-observe in the background so the fused request can fire
   the instant the transcript is final.
5. **Fuse the router with the first decision.** Do **not** make one call to
   classify intent and another to decide the action. Send intent + operation +
   target heads (+ yes/no head when waiting for confirmation) in **one** Jev
   request. If the intent turns out not to be a task/correction, discard the
   action heads. Output tokens are free, so the waste costs nothing.
6. **Field-value cache (prefetch text).** jev-ultrafast calls the text model
   synchronously on every `TYPE_TEXT`. Instead, whenever a new fingerprint
   appears with editable fields, fire **one background Mercury call** that
   returns values for **all** visible editable fields at once:
   `{field_index: value | null}`. `TYPE_TEXT` then reads from the cache.
   Cache key: `(goal_version, node_ref)`. Invalidate on goal change. On a
   cache miss, fall back to the synchronous call. A `null` value means the goal
   lacks that information → **missing-info gate** (ask the user).
7. **Normalizer extracts obvious slots.** Dates, numbers, currency, and known
   cities resolved by rules are passed to Mercury as facts, which shortens its
   job and its latency.
8. **Risk scoring off the critical path.** On each new fingerprint, a background
   Jev request scores clickable elements for irreversibility (boolean questions
   in parallel, one request). The executor checks the cache when a CLICK is
   chosen. If the score isn't ready yet, apply the keyword floor (§11) and, only
   then, a single synchronous boolean check.
9. **Compact state.** Send only visible text (already how snapshot works), cap
   page text (e.g., 6,000 chars), send the last ~10 actions. Fewer input tokens
   = faster Jev.
10. **Async end to end.** Port the model calls to `httpx.AsyncClient`. Run
    Browser Harness calls in a dedicated worker thread if the harness API is
    synchronous. Use `uvloop` and `orjson`.
11. **Keep jev-ultrafast's browser speed tricks.** One browser call per
    snapshot, waits capped at 200 ms (combobox suggestions) / 50 ms or two
    animation frames (everything else), focus emulation for hidden tabs.
12. **Timestamp everything.** Every event carries `t_ms` relative to key
    release. The status page shows a per-stage timing bar for the last command.

### 4.3 What NOT to do (it adds latency)

- No screenshots in the loop (only for debugging/recording).
- No chain-of-thought / reasoning on the text model (`reasoning_effort: instant`).
- No second model call to "double-check" routine decisions.
- No LLM-generated status messages — use templates.
- No sequential calls that could be parallel.

---

## 5. Tech stack

| Area | Choice | Notes |
|---|---|---|
| Language / tooling | Python 3.12, `uv` | Same as jev-ultrafast |
| Event loop / JSON | `asyncio` + `uvloop`, `orjson` | |
| HTTP | `httpx` with HTTP/2 (`httpx[http2]`), async | One pooled client per provider |
| Schemas | `pydantic` v2 | Events, configs, Jev payloads |
| Browser | **Browser Harness** (installed by jev-ultrafast `uv sync`) | Drives the user's Chrome via CDP |
| Page reading / execution | Vendored `snapshot.js` + `browser.py` | |
| Hotkey | `pynput` | Global push-to-talk (default F9, configurable) |
| Mic | `sounddevice` (+ `numpy`) | 16 kHz mono int16 |
| STT | **AssemblyAI Universal Streaming** (`universal-streaming-english`) | v3 WebSocket `wss://streaming.assemblyai.com/v3/ws`; `ForceEndpoint`; `keyterms_prompt` |
| STT SDK | `assemblyai` Python SDK (`assemblyai.streaming.v3`) or raw `websockets` | Raw WebSocket if the SDK adds latency or blocks the event loop |
| Decisions | **TypeSafe Jev**, pinned version (not `jev-latest`) | Direct to `api.typesafe.ai` |
| Field text | **Mercury** (`mercury-2.5`) called **directly** on Inception's OpenAI-compatible API `https://api.inceptionlabs.ai/v1` | `reasoning_effort: instant`, JSON mode (`response_format: json_object`); no OpenRouter hop |
| Normalizer | `dateparser`, `rapidfuzz`, custom gazetteer (YAML) | |
| Status page | `starlette` (or extend `demo.py`) + WebSocket + one static HTML file | `http://127.0.0.1:8766` |
| Logs | JSONL per session in `runs/` | |
| Tests | `pytest`, `pytest-asyncio` | |
| Lint | `ruff` | |

> Note: Mercury is a **text** model, not a speech model. STT is AssemblyAI.

---

## 6. Repository layout

```
voice-browser-agent/
├── README.md                 # this file
├── pyproject.toml
├── .env.example
├── config.yaml               # hotkey, thresholds, budgets, model versions
├── main.py                   # entry point: wires tasks, starts status page
│
├── agent/                    # vendored from jev-ultrafast (keep diffs small)
│   ├── agent.py              # + async, + goal-update hook, + event callback, + gates
│   ├── browser.py            # + blocked-field guard
│   ├── snapshot.js
│   ├── model.py              # + fused request builder, + cache-aware TYPE_TEXT
│   ├── questions.py          # + intent / confirm / risk rules
│   └── UPSTREAM.md           # upstream commit hash + list of local changes
│
├── core/
│   ├── interfaces.py         # SpeechToText, Decider, TextWriter, BrowserDriver protocols
│   ├── events.py             # pydantic event models
│   ├── bus.py                # asyncio pub/sub
│   ├── session.py            # state machine
│   ├── fused.py              # fused Jev request (intent + decision + confirm)
│   ├── start.py              # start resolver, navigation, tab following
│   ├── mercury.py            # direct Inception API client
│   ├── field_cache.py        # background Mercury prefetch
│   ├── risk.py               # keyword floor + background Jev risk scoring
│   └── timing.py             # per-stage timestamps
│
├── voice/
│   ├── hotkey.py
│   ├── mic.py
│   ├── stt_assemblyai.py
│   └── normalizer.py
│
├── data/
│   ├── gazetteer.yaml        # cities, airports, common names, aliases/misspellings
│   ├── sites.yaml            # known sites: url, aliases, about; search template
│   ├── profile.example.yaml  # template; real profile.yaml is git-ignored
│   └── profile.yaml          # your details (local only, git-ignored)
│
├── ui/
│   ├── server.py             # status page + WebSocket
│   └── index.html
│
├── verify/                   # optional per-task DONE verifiers
├── evals/
│   ├── tasks.yaml            # browser tasks with success checks
│   ├── voice/                # recorded commands (*.wav) + expected.yaml
│   ├── run_tasks.py
│   └── run_voice.py
│
├── runs/                     # JSONL logs (gitignored)
└── tests/                    # offline unit tests
```

---

## 7. Component specifications

### 7.1 Interfaces (`core/interfaces.py`)

Keep vendors swappable. Business logic must only use these.

```python
class SpeechToText(Protocol):
    async def start(self) -> None: ...                 # open + keep warm
    async def begin_utterance(self) -> None: ...       # key down
    async def send_audio(self, pcm: bytes) -> None: ...
    async def end_utterance(self) -> str: ...          # key up → ForceEndpoint → final text
    def partials(self) -> AsyncIterator[str]: ...      # live partial transcripts

class Decider(Protocol):
    async def decide(self, page, goal_ctx, history, extra_questions) -> FusedResult: ...

class TextWriter(Protocol):
    async def field_values(self, goal, fields, page_text, facts) -> dict[str, str | None]: ...
    async def answer_question(self, question, page_text) -> str: ...   # optional

class BrowserDriver(Protocol):
    async def observe(self) -> Page: ...
    async def fresh(self, page: Page) -> bool: ...
    async def act(self, action, page, text: str | None = None) -> None: ...
```

### 7.2 Hotkey + mic (`voice/hotkey.py`, `voice/mic.py`)

- Global hotkey via `pynput`; default **F9** (configurable). Key down →
  `begin_utterance()` + start streaming frames; key up → stop mic →
  `end_utterance()`.
- Ignore presses shorter than ~150 ms (accidental taps).
- 16 kHz mono int16 frames of ~40–100 ms pushed onto an `asyncio.Queue`
  (use `loop.call_soon_threadsafe` from the audio callback).
- macOS: needs Microphone and Accessibility/Input Monitoring permissions.

### 7.3 STT adapter (`voice/stt_assemblyai.py`)

- **AssemblyAI Universal Streaming, v3 WebSocket API**:
  `wss://streaming.assemblyai.com/v3/ws` with connection params
  `sample_rate=16000`, `encoding=pcm_s16le`,
  `speech_model=universal-streaming-english`.
- **Keep the connection open** across utterances; reconnect with backoff if the
  server closes it (sessions have duration limits — check the docs). Between
  utterances send nothing, or send silence if the server requires activity.
- **Audio:** raw 16-bit little-endian PCM, 16 kHz mono, sent as binary frames of
  ~50 ms. No encoding step.
- **Turn control (push-to-talk owns the turn):**
  - Disable early endpointing while the key is held: set
    `end_of_turn_confidence_threshold` high (e.g. 1.0) and `max_turn_silence`
    long (e.g. 2000–3000 ms) so a mid-sentence pause doesn't end the turn.
  - On key release, send `{"type": "ForceEndpoint"}` and wait for the `Turn`
    message with `end_of_turn: true`. That transcript is the final one.
- **Formatting vs latency:** `format_turns=true` returns punctuated, cased,
  number-formatted finals but may add a little latency. Benchmark both in M5;
  the normalizer can do its own number/date formatting if unformatted is faster.
- **Keyterms:** pass `keyterms_prompt` with the gazetteer's most important names
  (cities, airports, people, sites you use) to bias recognition. Keep the list
  short and relevant.
- Emit `TranscriptPartial` events from non-final `Turn` messages for the status
  page, and one `TranscriptFinal` with timings.
- **Verify exact parameter names, message types, limits, and the SDK's async
  behaviour against the current AssemblyAI streaming docs before implementing.**
- Keep the `SpeechToText` interface so the model can be swapped by config
  (e.g. `universal-3-5-pro` — see §18).

### 7.4 Normalizer (`voice/normalizer.py`)

Pure functions, no network, target < 5 ms. Output: cleaned text + extracted
`facts` dict.

- **Gazetteer fix-ups:** fuzzy-match tokens/bigrams against
  `data/gazetteer.yaml` (`rapidfuzz`, high threshold, e.g. ≥ 90) — "chen eye"
  → Chennai, "bangaluru"/"bengaluru"/"bangalore" → canonical form.
- **Dates:** "today", "tomorrow", "day after tomorrow", "next Friday", "on the
  25th", "25th October", and common Hindi/Tamil date words → absolute ISO date
  using the local timezone (Asia/Kolkata). Use `dateparser` with
  `PREFER_DATES_FROM="future"`.
- **Numbers / currency:** "four thousand two hundred" → 4200, "₹"/"rupees",
  "lakh", "crore".
- Never drop words it doesn't understand; it only rewrites what it's confident about.
- Unit-test heavily (it's pure and fast to test).

### 7.5 Fused Jev request (`core/fused.py`)

One request per user utterance (and the normal per-tick request during runs).

**On a new utterance** the request contains:

- `intent` — choice: `new_task`, `correction`, `confirm_yes`, `confirm_no`,
  `answer` (reply to a missing-info question), `cancel`, `page_question`, `noise`.
- The normal decision heads (`operation`, `click_target`, `type_text_target`,
  `select_target`) computed against the current page with goal context:
  ```json
  {"current_goal": "...", "latest_utterance": "...", "pending_question": "..."}
  ```
- The **start questions** (§7.11.2) when no run is active or the intent may be
  a new task: `start_here` (noul) and `start_site` (choice over `sites.yaml` +
  `none`). They ride in the same request — no extra round trip.
- When `WAITING_CONFIRM`, the `intent` criteria emphasise yes/no.

Session manager logic after the answer:

| Intent | Action |
|---|---|
| `new_task` | Set goal = utterance; resolve the start (§7.11.2). If starting on the current page, execute the returned decision immediately when confidence ≥ threshold; otherwise navigate first, then tick |
| `correction` | Goal = merge(current_goal, utterance) (template: "`{current_goal}`. Correction: `{utterance}`"); bump `goal_version`; invalidate field cache; execute returned decision |
| `confirm_yes` / `confirm_no` | Release or cancel the pending gated action |
| `answer` | Append the answer to the goal as a fact; resume |
| `cancel` | Stop after the current action completes |
| `page_question` | Ask Mercury with page text; show the answer on the status page |
| `noise` | Ignore; show "didn't catch that" |

**During a run** each tick sends only the decision heads (as jev-ultrafast does).

Rules:
- Validate every answer with the existing `validate_choice()` logic.
- If intent confidence < `INTENT_MIN_CONFIDENCE` (config, e.g. 0.6), show the
  transcript and ask the user to repeat — never guess on low confidence.

### 7.6 Field-value cache (`core/field_cache.py`)

- Trigger: new page fingerprint containing editable fields, or a goal change.
- One background Mercury call with: goal, normalizer `facts`, list of editable
  fields (`index`, label, role, current value), truncated visible page text.
- Required output: strict JSON `{"values": {"<index>": "<text>" | null}}`.
  Validate: only known indexes, strings ≤ 2,000 chars, otherwise discard.
- Cache by `(goal_version, node_ref)`; drop entries whose node disappears.
- On `TYPE_TEXT`: hit → type immediately; `null` → **missing-info gate**
  (state `WAITING_INFO`, status page shows the question, e.g. "Flying from
  where?"); miss/not ready → synchronous single-field call (jev-ultrafast
  behaviour).
- Never cache or type values for blocked fields (§11).

### 7.6a Mercury client (`core/mercury.py`)

Mercury is called **directly on Inception's API**, not through OpenRouter —
one less network hop and one less vendor.

- Endpoint: `POST https://api.inceptionlabs.ai/v1/chat/completions`
  (OpenAI-compatible), `Authorization: Bearer $INCEPTION_API_KEY`.
- Model: `mercury-2.5` (confirm the current id with `GET /v1/models`).
- Request settings for this project:
  - `reasoning_effort: "instant"` — no reasoning; lowest latency.
  - `response_format: {"type": "json_object"}` — JSON mode. The endpoint does
    not offer strict `json_schema` output, so **always validate the JSON in
    code** (known indexes only, string values, length caps) and discard on
    failure.
  - Small `max_tokens` (e.g. 512 for field values, 256 for page answers),
    `temperature: 0`, `stream: false`.
- Use one shared `httpx.AsyncClient(http2=True)` for Inception, warmed at
  startup. Timeout ~5 s; one retry on 429/5xx with short backoff.
- **Upstream patch required:** jev-ultrafast's `field_text()` builds an
  OpenRouter/DeepSeek-style reasoning parameter
  (`{"reasoning": {...}}` / `{"thinking": {...}}`). Replace it with
  `reasoning_effort` for Inception and read the key/base URL/model from the
  `INCEPTION_*` / `MERCURY_*` env vars. Record this in `agent/UPSTREAM.md`.
- Inception may serve from US regions only; measure the round trip from the
  laptop in M0. This call is mostly **off the critical path** (field-value
  prefetch), so its latency matters less than Jev's.
- **Verify parameter names (`reasoning_effort` values, JSON mode) against the
  current Inception docs before implementing.**

### 7.7 Risk scorer (`core/risk.py`)

- **Keyword floor (always on):** labels matching (case-insensitive) pay, buy,
  purchase, place order, book, confirm, submit, send, transfer, delete, remove,
  cancel booking, unsubscribe, checkout, proceed to pay, sign out, and their
  Hindi/Tamil equivalents present in the gazetteer → risky.
- **Background Jev scoring:** on each new fingerprint, one request with a
  boolean question per clickable element: "Clicking this commits an
  irreversible or external action (payment, booking, sending, submitting,
  deleting)". Cache probabilities by `node_ref`.
- **Gate rule:** risky if keyword floor OR `p_risky ≥ RISK_THRESHOLD`
  (config, **low**, e.g. 0.3 — err toward asking).
- Gated action → state `WAITING_CONFIRM`, status page shows the action label
  and any visible amount; a spoken yes/no resolves it. Timeout (e.g. 60 s) →
  cancel.

### 7.8 Agent core changes (`agent/`)

Keep upstream code recognisable; record every change in `agent/UPSTREAM.md`.

1. Make model calls async (`httpx.AsyncClient`, HTTP/2, shared clients).
2. `Agent.set_goal(text, version)` — applied between ticks only.
3. Event callback: emit `ActionDecided`, `ActionExecuted`, `PageObserved`,
   `RunStatusChanged` with timings.
4. Before executing CLICK: consult the risk scorer; if risky, raise/return a
   `GateRequired` result instead of acting.
5. Before executing TYPE_TEXT: consult the field cache; `null` → `GateRequired`
   (missing info); blocked field → refuse.
6. **No URL at construction.** `Agent(goal)` attaches to the active tab
   (§7.11.1); `navigate(url)` is a separate method called by the start
   resolver and by the `NAVIGATE` operation.
7. **New `NAVIGATE` operation** (§7.11.4) in `action_space()` and `choose()`.
8. **Follow new tabs** opened by the page (§7.11.5).
9. Keep: fingerprint checks, decision consumed before acting, log before
   re-observe, stuck detection, step budget.

### 7.9 Blocked fields (`agent/browser.py`)

Never type into a field if any of these hold: `type="password"`,
`autocomplete` in {`current-password`, `new-password`, `one-time-code`,
`cc-number`, `cc-csc`, `cc-exp`}, or a label/name matching password, OTP,
PIN, CVV, CVC, card number, Aadhaar, PAN, UPI PIN. Show "Please type this
yourself" on the status page and pause (`WAITING_INFO` with manual flag).

### 7.10 Status page (`ui/`)

Single local page at `http://127.0.0.1:8766`, fed by a WebSocket. Show:

- 🎤 **Heard:** live partial → final transcript (normalized version beneath).
- ⚙️ **Doing:** current step from a template ("Typing *Chennai* in *From*",
  "Clicking *Search*").
- ❓ **Needs:** pending gate prompt (confirm / missing info / type it yourself).
- ⏱️ **Timing bar:** STT, normalizer, Jev, execute for the last command.
- **History:** last ~20 actions with probabilities (reuse ideas from `demo.py`).
- Keep it one static HTML file with inline CSS/JS. No framework needed.

### 7.11 Dynamic start and navigation (`core/start.py`)

The user never pastes a URL. The agent decides where to start and can move
between sites. **Principle carried over from jev-ultrafast: a model never writes
a URL.** URLs come only from `data/sites.yaml`, from the current tab, or from a
search engine's results.

#### 7.11.1 Attach to the active tab

- On startup and on every new task, find Chrome's **currently focused tab** via
  CDP (Browser Harness / `Target.getTargets` + focus/visibility) and attach to
  it instead of opening a new owned tab.
- If Chrome has no usable tab (e.g. `chrome://` pages), open a blank tab.
- Config `navigation.open_in: current_tab | new_tab` (default `current_tab`).

#### 7.11.2 Start resolver (order of precedence)

1. **Named site (rules, no model).** The normalizer finds a site name or alias
   from `sites.yaml` in the utterance ("open ixigo", "on amazon", "make my
   trip") → use that site's URL. Strip the site phrase from the goal text.
2. **Current page.** Fused Jev noul `start_here`: *"This goal can be carried out
   on the current page or site."* If `p ≥ START_HERE_THRESHOLD` → act on the
   current tab immediately using the decision heads from the same request.
3. **Known site.** Fused Jev choice `start_site` over every entry in
   `sites.yaml` (criteria = its `about` text) plus `none`. Pick it if
   confidence ≥ threshold → navigate.
4. **Web search fallback.** `none` or low confidence → Mercury writes **only the
   search words** (JSON `{"query": "..."}`, validated, ≤ 120 chars) → navigate to
   the configured search URL with the query URL-encoded → the normal loop clicks
   the right result. Selecting a result is an ordinary `CLICK` on an observed
   link.

Emit `StartResolved {mode: named|here|site|search, site, url}`.

#### 7.11.3 `data/sites.yaml`

```yaml
sites:
  ixigo:
    url: https://www.ixigo.com
    aliases: [ixigo, exigo, ix a go]
    about: Flight, train and bus search and booking in India
  makemytrip:
    url: https://www.makemytrip.com
    aliases: [make my trip, mmt]
    about: Flights, hotels and holiday booking
  google_flights:
    url: https://www.google.com/travel/flights
    aliases: [google flights]
    about: Flight search and price comparison
  amazon:
    url: https://www.amazon.in
    aliases: [amazon]
    about: Buying products online
  maps:
    url: https://www.google.com/maps
    aliases: [google maps, maps]
    about: Places, directions, travel time
  gmail:
    url: https://mail.google.com
    aliases: [gmail, mail, email]
    about: Reading and sending email
search:
  url_template: https://www.google.com/search?q={query}
```

All `aliases` are also added to the gazetteer and to AssemblyAI
`keyterms_prompt` so site names are recognised correctly. The user edits this
file to add their own sites.

#### 7.11.4 `NAVIGATE` operation (moving between sites mid-task)

- Add `NAVIGATE` to the operation criteria: *"Go to a different website that is
  needed for the next part of the goal."*
- Target head `navigate_target`: choice over `sites.yaml` entries + `search`.
- `search` → Mercury writes the query (as in §7.11.2 step 4).
- Example: *"find the cheapest flight to Mumbai and email it to me"* →
  flight site … then `NAVIGATE → gmail`.
- Navigation is not risky by itself (no risk gate), but it is logged and shown
  on the status page.

#### 7.11.5 Tabs opened by the page

- Listen for new targets opened by the attached tab (CDP `Target.targetCreated`
  with `openerId`). If a click opens a new tab, **switch the agent to it**,
  re-observe, and emit `TabSwitched`.
- Keep a small tab stack; when that tab closes, return to the opener.
- Never switch to tabs the page did not open (the user's other tabs).

#### 7.11.6 Choice gate (ambiguous selections)

When the goal requires choosing among several similar results ("book me a
flight", "buy earphones") without a selection rule, do not let the agent pick
arbitrarily:

- Jev noul `needs_user_choice` on result-like pages: *"The goal does not say
  which of the visible options to choose, and choosing wrongly would matter."*
- If yes → `WAITING_INFO` with a prompt listing how many options are visible and
  examples of what the user can say ("the cheapest", "the 7 AM one", "the
  second one").
- The spoken answer is appended to the goal; the loop continues.
- Users can avoid the pause by saying the rule upfront ("book the cheapest
  flight…").

### 7.12 Local profile (`data/profile.yaml`)

Personal details used to fill forms (passenger details, delivery address),
kept **only on the laptop**:

```yaml
name: ""
gender: ""
date_of_birth: ""        # YYYY-MM-DD
email: ""
phone: ""
address: { line1: "", city: "", state: "", pincode: "" }
```

- Git-ignored; ship `profile.example.yaml` only.
- Passed to the field-value cache as facts; values are typed only into fields
  whose labels match (name, email, phone, address…) and **never** into blocked
  fields.
- Values are never sent to Jev (Jev only sees the page and the goal); they are
  sent to Mercury only when needed to map fields, and redacted in logs.
- If a needed value is missing from the profile → ask by voice.

---

## 8. Session state machine

```
            key down                 final transcript
  IDLE ─────────────► LISTENING ─────────────────────► ROUTING
   ▲                                                      │ fused answer
   │                                                      ▼
   │   done / blocked / cancelled                     RUNNING ◄───────────┐
   └──────────────────────────────── (terminal) ◄──────┤                  │
                                                        │ GateRequired     │ resolved
                                                        ▼                  │
                                        WAITING_CONFIRM / WAITING_INFO ────┘
```

- Speaking is allowed in **any** state. While `RUNNING`, an utterance is routed
  concurrently; goal changes / cancel apply **between ticks** (never mid-action).
- `LISTENING` does not pause the agent; a running task continues while the
  user speaks.
- Terminal states: `DONE`, `BLOCKED`, `CANCELLED`. A new `new_task` intent from
  any state starts a fresh run.
- Implement as an explicit enum + transition table; illegal transitions raise
  and are logged. Unit-test every transition.

---

## 9. Event types

All events are pydantic models with `session_id`, `seq`, `t_wall`, `t_ms`
(ms since the latest key release).

| Event | Key fields |
|---|---|
| `KeyDown` / `KeyUp` | — |
| `TranscriptPartial` | `text` |
| `TranscriptFinal` | `raw`, `stt_ms`, `provider` |
| `Normalized` | `text`, `facts`, `norm_ms` |
| `IntentClassified` | `intent`, `confidence`, `probabilities`, `jev_ms`, `model` |
| `GoalUpdated` | `goal`, `goal_version`, `reason` |
| `PageObserved` | `url`, `title`, `fingerprint`, `n_elements`, `observe_ms` |
| `StartResolved` | `mode` (named/here/site/search), `site`, `url` |
| `TabSwitched` | `from_target`, `to_target`, `reason` |
| `ActionDecided` | `operation`, `target`, `label`, `probability`, `confidence`, `jev_ms` |
| `FieldValuesCached` | `count`, `nulls`, `llm_ms` |
| `RiskScored` | `count`, `risky`, `jev_ms` |
| `GateOpened` | `kind` (confirm/info/manual), `prompt` |
| `GateResolved` | `kind`, `resolution` |
| `ActionExecuted` | `label`, `kind`, `text`, `page_changed`, `exec_ms` |
| `RunStatusChanged` | `status` |
| `Error` | `where`, `message`, `recoverable` |

---

## 10. Jev question catalogue

Design rule: each question is a **single, well-scoped judgement** a
knowledgeable person could make in a few seconds. Never hide several judgements
inside one question — split them and combine in code.

| Name | Type | When | Criteria / instruction |
|---|---|---|---|
| `intent` | choice | every utterance | new_task, correction, confirm_yes, confirm_no, answer, cancel, page_question, noise — each with a one-line definition |
| `operation` | choice | every tick | as jev-ultrafast |
| `click_target` / `type_text_target` / `select_target` | choice | every tick | as jev-ultrafast |
| `start_here` | noul | new task | "This goal can be carried out on the current page or site" |
| `start_site` | choice | new task | entries of `sites.yaml` (criteria = `about`) + `none` |
| `navigate_target` | choice | when `NAVIGATE` is a valid operation | `sites.yaml` entries + `search` |
| `needs_user_choice` | noul | result-like pages | "The goal does not say which visible option to choose, and choosing wrongly would matter" |
| `risky_<index>` | noul | background, per page | "Clicking this commits an irreversible or external action…" |
| `task_done` (optional) | noul | on `DONE` | "Every requirement of the goal is visibly satisfied on this page" — second opinion before declaring success |

Keep instructions in `agent/questions.py`. Pin the Jev model version in
`config.yaml` and log `result["model"]` on every call.

---

## 11. Safety rules (non-negotiable)

1. Model output only ever selects indexes from code-built lists.
2. Never type into blocked fields (§7.9). The user does it.
3. Every risky click (§7.7) requires a spoken **yes**. No timeout auto-approve.
4. Low-confidence intent → ask to repeat; never act on it.
5. Step budget per run (`MAX_STEPS`, default 40) and model-call budget.
6. Global kill switch: **Esc held for 1 s** or the utterance "stop" → cancel
   after the current action.
7. Keep all freshness / occlusion / consume-once guards from upstream.
8. API keys only in `.env`; never logged.
9. Optional: use a dedicated Chrome profile for the agent until you trust it.
10. **Models never write URLs.** Navigation targets come only from
    `sites.yaml`, the current tab, links observed on the page, or the search
    template with a model-written query string.
11. Profile values are local-only, typed only into matching non-blocked
    fields, never sent to Jev, and redacted in logs.
12. Only follow tabs opened by the attached page; never touch the user's other
    tabs.

---

## 12. Logging and tracing

- One JSONL file per session: `runs/YYYY-MM-DD/HHMMSS_<session>.jsonl`, one
  event per line (the events in §9, including full Jev request/response bodies
  and model version).
- Redact values typed into anything that looks sensitive (defence in depth).
- `scripts/replay.py <file>` prints a timeline with per-stage timings.
- `scripts/latency_report.py runs/` prints p50/p95 per stage across runs.
- Do not store raw audio by default (`SAVE_AUDIO=false`). When true, save WAVs
  next to the log for building the voice eval set.

---

## 13. Evaluation harness

### 13.1 Browser tasks (`evals/tasks.yaml`)

15–20 tasks you actually care about, each:

```yaml
- id: flights_one_way
  url: https://www.google.com/travel/flights?hl=en
  goal: "Find one-way flights from Chennai to Bangalore on 2026-10-02 for one adult"
  verify: verify/flights.py::check   # optional
  budget_s: 15
```

Include **no-URL tasks** that exercise the start resolver:

```yaml
- id: ixigo_named_site
  url: null                      # start from whatever tab is open
  start_page: https://www.youtube.com
  goal: "Open ixigo and find flights from Chennai to Mumbai on 2026-10-02"
  expect_start: named
- id: pick_known_site
  url: null
  start_page: https://en.wikipedia.org
  goal: "Find one-way flights from Chennai to Bangalore on 2026-10-02"
  expect_start: site
- id: current_page
  url: null
  start_page: https://www.amazon.in
  goal: "Search for wireless earphones under 2000 rupees"
  expect_start: here
- id: search_fallback
  url: null
  goal: "Find show timings for PVR Grand Galada Chennai"
  expect_start: search
```

`run_tasks.py` runs each task with **typed goals** (no STT) and reports success,
steps, p50/p95 per tick, Jev calls, text calls, cache hit rate, cost.

### 13.2 Voice commands (`evals/voice/`)

50–100 recorded WAVs (different rooms, noise, speaking speed, names) with
`expected.yaml`:

```yaml
- file: 001.wav
  expected_goal_contains: ["Chennai", "Bangalore", "2026-10-02"]
  expected_intent: new_task
```

`run_voice.py` replays audio through the STT adapter at real-time speed and
reports: entity accuracy, intent accuracy, STT latency p50/p95. Use it for the
**settings bake-off** (`format_turns` on/off, keyterms on/off, English vs
Universal-3.5 Pro) and for normalizer regressions.

### 13.3 Safety set

A local fixture page with risky buttons (Pay, Book, Delete, Send) and blocked
fields. **Gate recall must be 100%.** Run on every change.

### 13.4 Offline unit tests

Normalizer, state machine transitions, fused-answer handling, cache
invalidation, blocked-field detection, validate_choice. No network.

---

## 14. Milestones and exit criteria

Work in order. Do not start a milestone until the previous one's exit criteria
pass. Commit small, test as you go.

### M0 — Baseline (day 1)
- Clone jev-ultrafast, `uv sync`, run the Flights demo and 5 of the eval tasks.
- Vendor it into `agent/`, write `UPSTREAM.md`.
- **Exit:** baseline success rate and per-tick latency recorded in `runs/`.

### M1 — Async core + events + logging (days 2–3)
- `core/interfaces.py`, `core/events.py`, `core/bus.py`, `core/timing.py`.
- Async model calls, shared HTTP/2 clients, warm-up at startup.
- JSONL logging and `latency_report.py`.
- **Exit:** same success as M0, per-tick p50 equal or better, full event logs.

### M2 — Session manager with typed input (days 4–6)
- State machine, fused request (intent + decision), goal updates between ticks,
  cancel, typed-input CLI (stdin) standing in for voice.
- **Exit:** state-machine tests pass; corrections work in ≥ 90% of correction
  tests; low-confidence intents never act.

### M2a — Dynamic start and navigation (days 7–8)
- Attach to active tab, `sites.yaml`, named-site resolution, `start_here` /
  `start_site` in the fused request, search fallback, `NAVIGATE`, new-tab
  following, choice gate, `profile.yaml`.
- **Exit:** all no-URL eval tasks resolve to the expected start mode;
  zero model-written URLs in logs; the ixigo example (§3.3) reaches the payment
  hand-off with only the choice and confirm pauses.

> Later milestones shift by ~2 days.

### M3 — Speed layer (days 7–9)
- Field-value cache, background risk scorer, pre-snapshot.
- **Exit:** TYPE_TEXT cache hit rate ≥ 80% on the task set; per-tick p50 at
  least 20% lower than M1; safety set gate recall 100%.

### M4 — Gates and safety (days 10–11)
- Confirm and missing-info gates end to end, blocked fields, kill switch,
  budgets.
- **Exit:** safety set 100%; no action ever executed on a blocked field or an
  unconfirmed risky click.

### M5 — Voice input (days 12–15)
- Hotkey, mic, AssemblyAI adapter (warm WS, `ForceEndpoint`, keyterms),
  normalizer, gazetteer. Record the voice eval set; run the settings bake-off;
  choose the defaults.
- **Exit:** ≥ 95% entity accuracy on the voice set; **key release → first
  action p50 ≤ 1.0 s, p95 ≤ 2.0 s**.

### M6 — Status page and polish (days 16–18)
- Status page with transcript, step, prompts, timing bar, history.
- Reconnect logic, clear error messages, "didn't catch that" flow.
- **Exit:** a full day of real personal use without a stuck session; every
  failure visible and explained on the status page.

### M7 — Coverage (ongoing)
- Extend `snapshot.js` for what you actually hit (same-origin iframes and open
  shadow roots first). Add verifiers for your most common tasks.

---

## 15. Configuration

### `.env.example`

```bash
TYPESAFE_API_KEY=
TYPESAFE_MODEL=jev-1.13.0          # pin; verify current version in TypeSafe docs
TYPESAFE_BASE_URL=https://api.typesafe.ai

INCEPTION_API_KEY=                 # from platform.inceptionlabs.ai
INCEPTION_BASE_URL=https://api.inceptionlabs.ai/v1
MERCURY_MODEL=mercury-2.5          # verify current id via GET /v1/models
MERCURY_REASONING_EFFORT=instant

ASSEMBLYAI_API_KEY=

SAVE_AUDIO=false
```

### `config.yaml`

```yaml
hotkey: f9
min_press_ms: 150
audio: { sample_rate: 16000, frame_ms: 60 }
stt:
  url: wss://streaming.assemblyai.com/v3/ws
  speech_model: universal-streaming-english
  sample_rate: 16000
  encoding: pcm_s16le
  format_turns: true              # benchmark vs false
  end_of_turn_confidence_threshold: 1.0
  max_turn_silence_ms: 2500
  keyterms_from_gazetteer: 50
thresholds:
  intent_min_confidence: 0.6
  risk_threshold: 0.3
budgets: { max_steps: 40, max_model_calls: 100, confirm_timeout_s: 60 }
navigation:
  open_in: current_tab            # or new_tab
  sites_file: data/sites.yaml
  profile_file: data/profile.yaml
  start_here_threshold: 0.6
  start_site_min_confidence: 0.6
  follow_new_tabs: true
page_text_chars: 6000
history_actions: 10
timezone: Asia/Kolkata
status_port: 8766
```

---

## 16. Setup on the laptop

```bash
git clone <this repo> && cd voice-browser-agent
uv sync
cp .env.example .env            # fill in keys
uv run browser-harness --doctor # connect to Chrome; allow remote debugging when prompted
uv run python main.py           # starts agent + status page
# open http://127.0.0.1:8766, focus Chrome, hold F9 and speak
```

- macOS: grant Microphone and Accessibility / Input Monitoring to the terminal.
- Windows: `pynput` global hotkeys may need the terminal run as the same user
  session that owns Chrome.
- Development checks: `uv run ruff check . && uv run pytest`.

---

## 17. Instructions for Claude Code

1. **Read first.** Clone `browser-use/jev-ultrafast` and read `agent.py`,
   `model.py`, `browser.py`, `snapshot.js`, `questions.py`, `demo.py` before
   writing anything. Match their style: small, readable, explicit.
2. **Verify vendor APIs before coding against them.** AssemblyAI v3 streaming
   (connection params, `Turn` message fields, `ForceEndpoint`, session limits,
   keyterms), TypeSafe System One
   request/response (choice, noul, pinned model id), Inception Mercury model
   id. The shapes in this README are the expected ones; the docs win if they differ.
3. **Follow the milestones in order** and stop at each exit criterion to run the
   evals and report numbers.
4. **Never weaken the safety rules (§11)** or upstream guards to gain speed.
5. **Measure, don't assume.** Every optimisation must show up in
   `latency_report.py`. If one doesn't help, remove it.
6. **No site-specific logic** in the policy, prompts, or executor.
7. **Keep upstream diffs minimal** and list them in `agent/UPSTREAM.md`.
8. **Everything is async** except isolated blocking calls, which run in
   threads.
9. **Tests are offline.** Live evals and paid API calls only via `evals/`.
10. When something is ambiguous, prefer the simpler design and write the
    decision down in `DECISIONS.md`.

---

## 18. Known limitations and things to verify

- **TypeSafe is a hosted dependency.** Measure the round trip from the laptop
  (Chennai) early; it may dominate latency. Keep `Decider` swappable so a
  small-LLM decider with constrained JSON output can be added later.
- **Jev version drift.** Pin the model; re-run evals before changing it.
- **Unsupported pages.** Cross-origin iframes, closed shadow roots, canvas apps,
  uploads, and pop-ups won't work until `snapshot.js` is extended.
- **DONE is a claim, not proof.** Use verifiers for important tasks.
- **STT on names.** `keyterms_prompt` plus the gazetteer are the main defence;
  grow both from real misses found in the logs.
- **Language coverage.** `universal-streaming-english` is English only. The
  `universal-streaming-multilingual` model covers English, Spanish, French,
  German, Italian and Portuguese — **not Hindi or Tamil**. AssemblyAI's
  Universal-3.5 Pro streaming model code-switches across 18 languages including
  **Hindi** (not Tamil), and can be tried by changing `speech_model` only.
  Commands must be spoken in English for now.
- **Real booking sites (e.g. ixigo).** Expect offer pop-ups, app-download
  banners, cookie notices, login walls (OTP), and possibly iframes/shadow DOM
  in calendars or payment sections. Pop-ups are usually closable with a normal
  CLICK; login/OTP/payment are always handed to the user; unsupported widgets
  show up as `BLOCKED` in testing and are fixed in `snapshot.js` one by one.
  Staying logged in to the sites you use avoids most login walls.
- **Site layouts change.** No site-specific scripts means layout changes rarely
  break the agent, but run the live evals regularly.
- **Shared Chrome profile.** The agent sees your logged-in sessions; that's the
  point, and also why the gates exist.

---

## 19. References

- jev-ultrafast: https://github.com/browser-use/jev-ultrafast
- Browser Harness: https://github.com/browser-use/browser-harness
- TypeSafe docs (Jev, System One, fan-out): https://docs.typesafe.ai/introduction
- AssemblyAI streaming docs: https://www.assemblyai.com/docs/streaming/universal-3-pro
- AssemblyAI multilingual streaming: https://assemblyai.com/docs/streaming/multilingual-transcription
- Inception (Mercury) quick start: https://docs.inceptionlabs.ai/get-started/get-started
