# Implementation

What exists in this repo, how it fits together, and what is left to do.

- **[Plan.md](Plan.md)** is the build brief — the spec. This file records what
  was actually built against it.
- **[DECISIONS.md](DECISIONS.md)** records choices made where the Plan was
  ambiguous, and the vendor facts verified against live docs.
- **[ERRORS.md](ERRORS.md)** records every bug hit while building: what it was,
  what caused it, and how it was fixed.
- **[agent/UPSTREAM.md](agent/UPSTREAM.md)** lists every local change to the
  vendored `jev-ultrafast` core.

Status: **M0–M2a complete and verified live end to end (2026-09-26)** — all
three vendors, the browser layer, and one full typed-goal run through the whole
pipeline. 222 offline tests pass and `ruff` is clean. See §11.

---

## 1. At a glance

| | |
|---|---|
| Language / tooling | Python 3.12, `uv`, `ruff`, `pytest` (+`pytest-asyncio`) |
| Python source | ~3,900 lines across `agent/`, `core/`, `voice/`, `ui/`, `scripts/`, `evals/` |
| Tests | 222, all offline — `tests/` cannot reach the network (enforced by a fixture) |
| Vendored | `browser-use/jev-ultrafast` @ `1231850a0bf1a0c0341fe408ef1668dbbfdfac46` (MIT) |
| External services | TypeSafe (Jev), Inception (Mercury), AssemblyAI (STT) — **all three keys verified working** |
| Entry point | `main.py` — voice by default, `--type` for stdin |
| Status page | `http://127.0.0.1:8766` |

### Running it

```bash
uv sync                              # done: .venv exists, uv.lock committed
cp .env.example .env                 # then fill in three keys (§9)
cp data/profile.example.yaml data/profile.yaml
uv run browser-harness --doctor      # connect to Chrome
uv run python main.py                # voice: hold F9
uv run python main.py --type         # typed goals on stdin, no microphone
uv run pytest                        # 200 offline tests
uv run ruff check .
```

---

## 2. Repository map

Every file, what it is for, and its size.

### Vendored agent core — `agent/` (1,332 lines)

| File | Lines | Role |
|---|---|---|
| `snapshot.js` | 107 | **Unchanged from upstream.** One atomic DOM read: visible interactive controls with role/label/value/state, visible text, per-node guards, and a page fingerprint |
| `browser.py` | 379 | CDP execution through Browser Harness + the async facade, active-tab attach, navigation, blocked-field guard, tab following |
| `agent.py` | 476 | The tick loop: predict → gate → act → observe. Goal updates, gates, `NAVIGATE`, stuck detection, budgets |
| `model.py` | 285 | Jev request building and answer validation; `action_space()`, `choose()`, `ask()` |
| `questions.py` | 84 | Every instruction block sent to a model, in one place |
| `UPSTREAM.md` | 82 | The upstream commit and each local change, with its reason |

### Business logic — `core/` (1,092 lines)

| File | Lines | Role |
|---|---|---|
| `session.py` | 436 | The state machine. Owns the goal, routes every utterance, runs the tick loop, manages gates |
| `mercury.py` | 179 | Inception client: batched field values, search words, page answers. Validates every response |
| `events.py` | 161 | 20 pydantic event types + log redaction |
| `risk.py` | 128 | Keyword floor + background Jev risk scoring |
| `field_cache.py` | 108 | Pre-computed field values keyed by `(goal_version, node)` |
| `config.py` | 90 | `.env` loader + typed settings from `config.yaml` |
| `start.py` | 88 | Start resolver: named site → current page → known site → web search |
| `fused.py` | 88 | Builds the fused question heads (intent, start, choice gate, task done) |
| `sensitive.py` | 57 | **The single source of truth** for what must never be typed |
| `timing.py` | 56 | Per-stage timing, zeroed at key release |
| `http.py` | 55 | One warm HTTP/2 client per provider |
| `bus.py` | 53 | asyncio pub/sub; publishing never blocks |
| `interfaces.py` | 47 | The four swappable seams |
| `log.py` | 30 | One JSONL file per session |

### Voice input — `voice/` (568 lines)

| File | Lines | Role |
|---|---|---|
| `normalizer.py` | 249 | Gazetteer fixes, date resolution, numbers, currency, slot extraction |
| `stt_assemblyai.py` | 188 | AssemblyAI Universal Streaming v3 over a warm WebSocket |
| `hotkey.py` | 68 | Global push-to-talk + the Esc kill switch |
| `mic.py` | 63 | 16 kHz mono int16 capture into the event loop |

### Frontend — `ui/` (291 lines)

| File | Lines | Role |
|---|---|---|
| `index.html` | 207 | The whole status page: markup, CSS, and the WebSocket client |
| `server.py` | 84 | Starlette routes + the event WebSocket, on a pre-bound socket |

### Data, tests, tooling

| Path | Role |
|---|---|
| `data/sites.yaml` | 14 known sites + the search template. **The only place a URL can come from** |
| `data/gazetteer.yaml` | Cities, airports, and the risky-label keyword floor |
| `data/profile.example.yaml` | Template for the git-ignored `profile.yaml` |
| `tests/` (5 files, 200 tests) | Offline suite — see §7 |
| `scripts/replay.py` | Prints one session's timeline from its JSONL log |
| `scripts/latency_report.py` | p50/p95 per stage across runs, and the §4.1 budget verdict |
| `evals/` | Live checks: 18 browser tasks, the voice bake-off, the safety fixture |

---

## 3. How one turn flows

```
F9 down ──► Hotkey ──► Session.key_down()  ──►  state: LISTENING
            │                                   (a running task keeps going)
            └─► Mic.begin() ─► frames ─► STT.send_audio() ═══► AssemblyAI (warm WS)
                                                                    │ partials
                                                                    ▼
                                                          TranscriptPartial ─► status page
F9 up ───► Clock.release()          ← t=0 for the whole latency budget
       ├─► drain remaining frames ─► STT
       ├─► STT.end_utterance() ─► ForceEndpoint ═══► final transcript
       ├─► normalize()  (~0.35 ms, pure rules)
       │      "…chen eye to bangalore next friday"
       │      → "…Chennai to Bengaluru 2026-10-02" + facts{from,to,date}
       ├─► "stop"? ──► cancel immediately, no model call
       └─► Session.route()
              │
              └─► ONE Jev request carrying every head at once:
                    intent · operation · click_target · type_text_target
                    · select_target · navigate_target · start_here · start_site
              │
              ├─ intent confidence < 0.6 ──► ask the user to repeat, act on nothing
              └─ intent = new_task
                    ├─ StartResolver: named site? → here? → known site? → search
                    ├─ mode "here":  execute the decision that came back in the
                    │                SAME request, inside the awaited path
                    └─ otherwise:    navigate first, then start the loop
                          │
                          ▼
                    loop: tick → tick → tick   (goal changes land between ticks)
                          │
                          ├─ TYPE_TEXT → field cache hit (≈0 ms) │ null → ask │ miss → one Mercury call
                          ├─ CLICK     → choice gate → risk gate → execute
                          ├─ NAVIGATE  → URL from sites.yaml, never from a model
                          └─ DONE / BLOCKED / stuck → terminal
```

Everything on that path emits an event; the events are both the status page's
feed and the JSONL log, so a run can be replayed exactly.

---

## 4. Backend: what each part does

### 4.1 The state machine (`core/session.py`)

Nine states and an **explicit transition table**; anything not in the table
raises `IllegalTransition` and logs an `Error` event.

```
IDLE · LISTENING · ROUTING · RUNNING · WAITING_CONFIRM · WAITING_INFO
DONE · BLOCKED · CANCELLED          (last three terminal)
```

Behaviours worth knowing:

- **Speaking is allowed in every state.** `key_down()` only moves to `LISTENING`
  from idle/terminal states — while a task is running it deliberately does
  nothing, so talking never pauses the agent.
- **The kill switch never waits for a model.** `"stop"`, `"cancel"`, `"abort"`
  and friends are matched on the normalized text and cancel immediately.
  Everything else is routed by Jev.
- **Goal changes are queued, not applied.** `Agent.set_goal()` stores the new
  goal; `Agent.apply_goal()` runs at the top of the next tick and bumps
  `goal_version`, which invalidates the field cache. An action already in flight
  always finishes against the goal it started with.
- **Gates.** When a tick raises `GateRequired`, the loop stops and the session
  moves to `WAITING_CONFIRM` (risky click) or `WAITING_INFO` (missing value,
  blocked field, ambiguous choice) and arms a 60 s timer. The timer **cancels**;
  it never approves.
- **The first action is inside the awaited path.** For a `new_task` that starts
  on the current page, `Session.new_task()` executes the decision that rode back
  with the intent answer, rather than handing it to the background loop — so the
  key-release → first-action budget covers it and is measurable in one place.

Intent handling, one line each:

| Intent | What happens |
|---|---|
| `new_task` | Stop any run, set the goal, resolve the start, act (here) or navigate then loop |
| `correction` | `goal = "{current}. Correction: {utterance}"`, bump version, invalidate cache, resume |
| `confirm_yes` / `confirm_no` | Release exactly one gated action, or cancel the run |
| `answer` | Append the answer to the goal as a fact, drop the gate, resume |
| `cancel` | Stop after the action in flight |
| `page_question` | Mercury answers from page text; nothing is clicked |
| `noise` | "Didn't catch that" |

### 4.2 The agent loop (`agent/agent.py`)

Upstream's loop, kept recognisable, made async, with gates inserted between the
decision and the execution. **Every upstream safety property is intact**:

- model output only ever selects an index from a list the code built;
- every target resolves to an observed DOM node;
- freshness is checked before acting and again immediately before input;
- occluded controls are rejected;
- invalid model output executes nothing;
- **the decision is consumed before acting**, so a retry can never double-click;
- execution is logged *before* re-observing, so a stale observation cannot erase
  a real action.

Added on top: `set_goal`/`apply_goal`, `gate_for()`, `execute_navigate()`,
`after_action()` (tab following + stuck detection), an event callback on every
step, and a per-run step and model-call budget.

### 4.3 The fused request (`core/fused.py`, `agent/model.py`)

One Jev round trip carries: `operation`, one target head per valid operation,
plus whichever of `intent`, `start_here`, `start_site`, `needs_user_choice` the
caller attached. Unused heads are discarded — Jev bills input tokens only, so
extra heads are nearly free, and a second round trip is not.

Validation is per-head and strict. `validate_choice()` is upstream's, unchanged:
probabilities must sum to ~1, the choice must be the argmax, every id must
exist. `validate_noul()` was added for boolean heads and accepts either
documented shape; **anything else is treated as "no answer", never as "yes"**.
An invalid head is dropped, never guessed.

### 4.4 Dynamic start (`core/start.py`)

Order of precedence, exactly as Plan §7.11.2:

1. **Named site** — the normalizer found a `sites.yaml` alias ("open ixigo…").
   Rules only, no model. The site phrase is stripped from the goal.
2. **Current page** — `start_here` ≥ 0.6 → act on the tab already open, using
   the decision from the same request.
3. **Known site** — `start_site` over `sites.yaml` entries + `none`, confidence
   ≥ 0.6 → navigate.
4. **Web search** — Mercury writes *only the query words* (≤ 120 chars,
   rejected if it contains `://`), URL-encoded into our own template.

**No model ever writes a URL.** URLs come from `sites.yaml`, the current tab,
links observed on the page, or the search template. `Browser.navigate()` also
refuses anything that is not `http(s)://`.

### 4.5 Speed layer (`core/field_cache.py`, `core/risk.py`, `core/http.py`)

| Optimisation | How it is implemented |
|---|---|
| Warm connections | One `httpx.AsyncClient(http2=True)` per provider, warmed at startup; the AssemblyAI socket stays open with `KeepAlive` |
| Force the endpoint | Key release sends `ForceEndpoint`; the model's own endpointing is turned down so a pause cannot cut the user off |
| Fuse router + decision | §4.3 — one request instead of two |
| Field-value prefetch | One background Mercury call per (fingerprint, goal_version) fills every visible editable field; `TYPE_TEXT` then reads memory |
| Risk scoring off-path | One background Jev request scores every clickable element on a new fingerprint |
| Pre-warm on the "here" path | Cache and risk scoring start while the first action is still executing |
| Compact state | Page text capped at `page_text_chars`, history at `history_actions` |
| Async end to end | One dedicated worker thread for the synchronous CDP calls, so the loop never blocks and CDP ordering is preserved |

Cache semantics are three-valued and this matters: **HIT** → type it;
**NULL** → the goal genuinely lacks that value → missing-info gate;
**MISS** → not ready → fall back to one synchronous Mercury call.

### 4.6 Safety (`core/sensitive.py`, `core/risk.py`, `agent/browser.py`)

| Rule (Plan §11) | Where it lives |
|---|---|
| Model output only selects indexes | `action_space()` + `validate_choice()` |
| Never type into blocked fields | `core/sensitive.py`, enforced in the gate, the cache **and** the in-page JS |
| Risky clicks need a spoken yes | `RiskScorer` — keyword floor OR `p ≥ 0.3` |
| Low-confidence intent never acts | `read_intent()` returns `None` below 0.6 |
| Step and model-call budgets | `Agent.max_steps`, `Agent.max_model_calls` |
| Kill switch | Esc held 1 s, or the word "stop" — neither needs a model |
| Upstream guards kept | §4.2 |
| Keys only in `.env` | `.gitignore`; `redact()` strips key-shaped fields from logs |
| Models never write URLs | §4.4 |
| Profile is local-only | Never sent to Jev; to Mercury only to map fields; redacted in logs |
| Only follow tabs the page opened | `Browser.opened_tabs()` filters on `openerId` |

The keyword floor is deliberately **not overridable by the model**: a label that
looks like a commitment is gated even when Jev scores it safe. The model can
add risk, never remove it. `tests/test_safety_set.py` asserts exactly that.

### 4.7 The normalizer (`voice/normalizer.py`)

Pure, no network, **measured at 0.35 ms** against a 5 ms budget.

| Input | Output |
|---|---|
| `"find flights from chen eye to bangalore next friday"` | `"find flights from Chennai to Bengaluru 2026-10-02"` + `{cities, date, from, to}` |
| `"earphones under four thousand two hundred rupees"` | `"earphones under ₹4200"` + `{amount: 4200}` |
| `"book a cab tomorrow to hyderabad for two lakh rupees"` | `"book a cab 2026-09-26 to Hyderabad for ₹200000"` |
| `"open ixigo and book a flight"` | `{site: ixigo}`; the phrase is strippable from the goal |

Order matters and is deliberate: names → dates → spoken numbers → lakh/crore →
currency, so `"four thousand rupees"` is digits by the time the currency rule
sees it. Unknown words are never dropped.

### 4.8 Voice capture (`voice/hotkey.py`, `voice/mic.py`, `voice/stt_assemblyai.py`)

- `pynput` listener thread → `loop.call_soon_threadsafe` → the event loop.
  Presses under 150 ms are ignored.
- `sounddevice` `RawInputStream` kept open for the whole session (opening one
  costs ~100 ms); frames cross to the loop through a bounded queue that drops
  rather than blocking the audio thread.
- The STT adapter keeps one warm WebSocket, reconnects with backoff (sessions
  auto-close after three hours), sends `KeepAlive` while idle, and on release
  sends `ForceEndpoint` and waits for `end_of_turn`. With `format_turns` on it
  waits a further 350 ms for the punctuated version.

---

## 5. Frontend: the status page

One file, `ui/index.html` (207 lines) — markup, CSS and client in one place, no
framework, no build step, no external requests.

**Layout.** A header with the live state chip and current URL, then four panels
on the left and the history on the right (single column under 900 px):

| Panel | Shows |
|---|---|
| 🎤 **Heard** | Live partial transcript in grey, replaced by the final in white; the normalized version and the current goal beneath |
| ⚙️ **Doing** | The current step from a template — never model-written prose |
| ❓ **Needs** | The pending gate prompt: "Book now — say yes or no.", "What should I put in From?", "Please type OTP yourself." |
| ⏱️ **Last command** | A proportional timing bar — STT, normalize, Jev, execute — with the per-stage milliseconds and the total |
| **History** | The last 20 events with probabilities, newest first |

**Transport.** `ui/server.py` serves the page and a `/events` WebSocket that
replays the bus. `/status` returns the session snapshot as JSON. Two details
that were bugs first:

- The socket is **bound before uvicorn starts**, because uvicorn calls
  `sys.exit(3)` on a bind failure — a busy port would otherwise kill the agent.
  Now it degrades to "running without the status page".
- The WebSocket handler races a `receive()` against the event queue. Without it
  the handler sits in `queue.get()` forever, never notices the page closing, and
  blocks shutdown — which it did, until it was fixed.

**Theming.** Light and dark via `prefers-color-scheme`. The state chip is
colour-coded: blue running, green listening, amber waiting on the user, red
blocked.

The client reconnects every second if the socket drops, so restarting the agent
does not require reloading the page.

---

## 6. Events and logging

20 typed events (`core/events.py`), each carrying `session_id`, `seq`,
`t_wall`, and **`t_ms` measured from the last key release**:

```
KeyDown KeyUp TranscriptPartial TranscriptFinal Normalized IntentClassified
GoalUpdated PageObserved StartResolved TabSwitched ActionDecided
FieldValuesCached RiskScored GateOpened GateResolved ActionExecuted
RunStatusChanged StateChanged Notice Error
```

Every event goes to `runs/YYYY-MM-DD/HHMMSS_<session>.jsonl` through
`redact()`, which strips anything key-shaped. Two readers:

```
$ uv run python scripts/replay.py runs/2026-09-25/011959_29c17a5b0b34.jsonl
      0ms  KeyDown
      1ms  KeyUp                held 900ms
      1ms  TranscriptFinal      "find flights from chennai to mumbai next friday" (240ms, assemblyai)
     15ms  Normalized           "find flights from Chennai to Mumbai 2026-10-02" {...}
     24ms  IntentClassified     new_task p=0.90 (42ms jev-test)
     24ms  StartResolved        here
     25ms  ActionExecuted       fill From = Chennai
   1787ms  ActionExecuted       click Search
   1789ms  GateOpened           confirm: Book now — say yes or no.

$ uv run python scripts/latency_report.py runs/
stage                                n      p50      p95      max
stt                                  1     240ms    240ms    240ms
key_release_to_first_action          1      25ms     25ms     25ms
Budget (Plan §4.1): p50 25ms / 1000ms PASS, p95 25ms / 2000ms PASS
```

That run is from an **offline dry run** with a fake browser and scripted
decisions — it proves the pipeline and the tooling, not the latency. Real
numbers need keys (§9).

---

## 7. Tests — 200, all offline

`tests/conftest.py` installs an autouse fixture that stubs `post_json` and
`complete_json` to raise. Any test that reaches for a paid API fails loudly.
(This found real accidental network calls: the suite went from 4.4 s to 1.5 s
once they were cut.)

| File | Tests | Covers |
|---|---|---|
| `test_state_machine.py` | 79 | Every legal transition, every illegal one, logging, no-op self-transitions, terminal restarts, "listening does not pause a run" |
| `test_guards.py` | 42 | `validate_choice` rejections, both `noul` shapes, blocked-field recognition, keyword floor, `action_space` indexing, code-owned NAVIGATE URLs |
| `test_safety_set.py` | 31 | The safety set (Plan §13.3): every risky label gated, every safe label not, every blocked field refused, the executor refusing even when the gate is bypassed, and the floor beating a model that says "safe" |
| `test_normalizer.py` | 28 | Aliases, dates, spoken numbers, currency, slots, site stripping, empty input, and the < 5 ms budget |
| `test_session_routing.py` | 20 | Start modes, low-confidence refusal, the model-free kill switch, corrections, cache invalidation, all four gate kinds, gate timeout, one-action-per-yes |

Fakes rather than mocks: `FakeBrowser` implements the `BrowserDriver` seam and
records what it was asked to do, so assertions read as "what did the agent
actually do to the browser".

`test_safety_set.py` parses the labels **out of `evals/safety_fixture.html`**
and asserts they match the list under test, so the offline check and the live
fixture cannot drift apart.

---

## 8. What was verified, corrected, and fixed

### Vendor facts checked against live docs (Plan §17.2)

The AssemblyAI v3 streaming reference was read before the adapter was written.
Two details in the Plan needed correcting:

| Plan said | Docs say |
|---|---|
| (unspecified auth) | API key in `Authorization` with **no** `Bearer` prefix |
| `keyterms_prompt` with "the gazetteer's most important names" | A **JSON-encoded array** in the query string; max 100 terms, ≤ 50 chars each |

Confirmed as written: the v3 URL, `pcm_s16le` @ 16 kHz, `format_turns`,
`end_of_turn_confidence_threshold`, `ForceEndpoint`, and the `Turn` fields.
Newly used: `max_turn_silence` (the Plan's `max_turn_silence_ms` maps to it),
`KeepAlive`, and the three-hour session limit that drives reconnection.
With `format_turns` on, the formatted transcript arrives as a **second** `Turn`.

TypeSafe and Inception could not be verified without keys — see §9.

### Bugs found and fixed while building

| Bug | Why it mattered | Fix |
|---|---|---|
| The blocked-field list existed twice, in the executor and the cache, and had already drifted ("security code" was in one only) | A field could be refused by one layer and typed by another | One list in `core/sensitive.py`, imported by the executor, the in-page JS guard and the cache |
| Substring matching blocked "Pincode" (contains "pin") | Ordinary address forms would stall on a manual gate | Whole-word regex, shared with the JS guard so the two cannot drift |
| A `null` field value raised on the synchronous path | The tick failed with an error instead of asking the user | `single_field_value()` returns `None` for a genuine null; both paths open the same gate |
| `FieldCache.prune()` read `action["node"]` on control actions that have none | `KeyError` on any page with a scroll or wait control — i.e. all of them | Skip actions without a node |
| `RiskScorer` and `FieldCache` caught `CancelledError` in a tuple with `Exception` | Cancellation was swallowed, so cache invalidation could leave tasks half-dead | Re-raise cancellation, swallow only real failures |
| uvicorn `sys.exit(3)` on a busy status port | A stray process would take the whole agent down | Bind the socket first; degrade to no status page |
| The WebSocket handler never noticed a disconnect | Shutdown hung forever | Race a `receive()` against the queue |
| `NAVIGATE_TARGET` instructions were written but never sent | The navigate head was getting element-targeting rules | Wired into `build_questions()` |
| Tests cancelled the run loop before it ran | Several loop tests were silently no-ops | `drain()` awaits real completion |

### Deviations from the Plan, and why

All recorded in DECISIONS.md. The ones that change behaviour:

1. **"Next Friday" = the next occurrence strictly in the future.** dateparser
   returned `None` for the phrase; weekdays are now resolved by one rule that
   can be stated in a sentence and corrected by voice.
2. **`intent_min_confidence` also gates the fused first action.** The Plan says
   "confidence ≥ threshold" but defines no second threshold and `config.yaml`
   has no key for one. A below-threshold decision is not discarded — the loop
   re-predicts.
3. **Choice-gate heuristic.** The Plan says to ask `needs_user_choice` "on
   result-like pages" without defining the term. Code-side: ≥ 8 distinct
   clickable elements, asked at most once per goal version.
4. **Device-metrics override only on tabs we open.** Forcing 1120×780 on the
   user's own visible tab would be rude and visible.
5. **Tab following polls `Target.getTargets` for `openerId`** rather than
   relying on CDP events, which Browser Harness does not guarantee to deliver.
6. **Windows has no uvloop**, so this laptop runs the default selector loop.

---

## 9. Next steps

### Step 0 — the blocker: three API keys

Nothing below can be measured without these. Put them in `.env`:

| Key | From | Used for |
|---|---|---|
| `TYPESAFE_API_KEY` | <https://typesafe.ai> | Every decision (Jev) |
| `INCEPTION_API_KEY` | <https://platform.inceptionlabs.ai> | Field text, search words, page answers (Mercury) |
| `ASSEMBLYAI_API_KEY` | <https://assemblyai.com> | Speech to text |

Then `uv run browser-harness --doctor` to connect Chrome.

### Step 1 — verify the two unverified vendors *(first live session)*

- [ ] **TypeSafe `noul` response shape.** `validate_noul()` accepts either
      documented form and treats anything else as *no answer*. Log one real
      boolean answer, confirm the shape, and tighten the parser.
- [ ] **Inception model id.** `GET /v1/models`; confirm `mercury-2.5` and that
      `reasoning_effort: "instant"` is accepted.
- [ ] **Pin the Jev model.** `.env` ships `TYPESAFE_MODEL=jev-1.13.0` — confirm
      the current version and pin it deliberately.
- [ ] Measure the TypeSafe round trip from this machine. It may dominate the
      budget; if it does, the `Decider` seam is already there to swap.

### Step 2 — M0/M1 exit criteria: the baseline that does not exist yet

- [ ] `uv run python evals/run_tasks.py` — record success rate, steps, tick p50.
- [ ] `uv run python scripts/latency_report.py runs/` — the first real numbers.
- [ ] Commit those numbers so later work has something to beat.

### Step 3 — M3 exit criteria: prove the speed layer earns its place

Plan §17.5: every optimisation must show up in `latency_report.py`, or be
removed.

- [ ] TYPE_TEXT cache hit rate ≥ 80% across the task set. (Nothing currently
      emits a hit/miss counter — **add a `FieldCacheHit` event or extend
      `ActionExecuted`**, since `text_helper == "cache"` is the only current
      signal.)
- [ ] Per-tick p50 at least 20% below the M1 baseline.
- [ ] Confirm background risk scoring usually beats the executor to the click;
      measure how often the synchronous fallback fires.

### Step 4 — M5 exit criteria: the voice loop end to end

- [ ] Record 50–100 WAVs (16 kHz mono) and fill in `evals/voice/expected.yaml`.
- [ ] Run the bake-off: `--no-format`, `--no-keyterms`, `--model
      universal-3-5-pro`. Choose defaults from the numbers, not the docs.
- [ ] Hit ≥ 95% entity accuracy and **key release → first action p50 ≤ 1.0 s,
      p95 ≤ 2.0 s**.
- [ ] Verify the Windows hotkey path: `pynput` global hotkeys need the terminal
      in the same user session that owns Chrome.

### Step 5 — finish what is built but not wired

- [ ] **`task_done` verifier.** `core/fused.py:59` builds the head; nothing
      calls it. Wire it as a second opinion before declaring `DONE` (Plan §10,
      optional but cheap).
- [ ] **`verify/` is empty.** Add per-task verifiers for the tasks you actually
      care about — `DONE` is a claim, not proof.
- [ ] **Profile → field cache.** `data/profile.yaml` is loaded by config but is
      not yet merged into the facts passed to Mercury. Passenger-detail forms
      will ask questions they should not need to ask.
- [ ] **`Browser.return_to_opener()`** exists and is tested by nothing; no
      caller invokes it when a followed tab closes.
- [ ] **`UpdateConfiguration`** on the STT socket would let keyterms follow the
      goal mid-session. Not wired.

### Step 6 — M6 and real use

- [ ] A full day of personal use without a stuck session.
- [ ] Every failure visible and explained on the status page.
- [ ] Grow `data/gazetteer.yaml` and `keyterms_prompt` from real misses in
      `runs/` — that is the main defence for names.
- [ ] Extend `snapshot.js` only for what you actually hit: same-origin iframes
      and open shadow roots first (calendars and payment sections on Indian
      booking sites are the likely first offenders).

### Housekeeping

- [ ] Uncommitted: `Implementation.md`, and the `agent/model.py` change that
      wires `NAVIGATE_TARGET` into the navigate head. Everything else is in
      `605e92b base repo setup`.
- [ ] Decide whether to run against a dedicated Chrome profile until the gates
      have earned trust (Plan §11.9).
- [ ] `data/profile.yaml` does not exist yet; copy the example and fill it in.

---

## 10. Known risks

| Risk | Current mitigation |
|---|---|
| TypeSafe latency from India may dominate the budget | `Decider` is a swappable seam; measure in Step 1 before optimising anything else |
| The `noul` shape is assumed, not confirmed | An unparsable boolean is "no answer", so risk scoring falls back to the keyword floor rather than approving |
| Real booking sites bring pop-ups, login walls, iframes | Pop-ups close with an ordinary CLICK; login/OTP/payment are always handed to the user; unsupported widgets surface as `BLOCKED` |
| English only | `universal-streaming-english`; Hindi needs `universal-3-5-pro` (a `speech_model` change), Tamil is unsupported by any streaming model |
| The agent shares your logged-in Chrome profile | That is the point, and why the gates exist. `tests/test_safety_set.py` must stay at 100% |
| No uvloop on Windows | Measure the cost before chasing smaller wins |

---

## 11. Live verification — 2026-09-26

Step 0 and Step 1 of §9 are done. Eleven API calls total, chosen to maximise
information per call; no eval suite was run.

### Results

| Check | Result |
|---|---|
| Inception `GET /v1/models` | `["mercury-2", "mercury-2.5"]` — the pinned id is correct (free call) |
| Mercury batch field values | Works; `{"1": "Chennai", "2": null}` in 623 ms |
| Mercury single field value | Works; `'Chennai'` in 573 ms |
| Mercury single, value absent | Returns `None` in 469 ms → the missing-info gate opens, nothing invented |
| Mercury search words | `'PVR Grand Galada Chennai show timings'` — words only, no URL |
| TypeSafe auth + `jev-1.13.0` | Valid; the pinned model answers |
| Jev choice heads | Match `validate_choice()` exactly |
| Jev boolean (`noul`) heads | **Shape was wrong in our validator — see below** |
| Jev latency | Cold **1405 ms**, warm **299–345 ms** (budget ≤ 400 ms) |
| AssemblyAI connection | All parameters accepted, including 29 keyterms; ~980 ms handshake |

### The bug this found

Boolean heads come back as `{"type": "noul", "noul": 0.9}` — a bare probability
under `noul`, with no `confidence`. Our validator expected `probability` or a
yes/no `probabilities` map, so **it rejected every boolean answer**.

The defensive design in DECISIONS.md §2 held exactly as intended: an unparsable
boolean became *no answer*, never a wrong "yes", so nothing unsafe happened. But
the consequence was that three features were silently inert against the live
API — `start_here` (so every task fell through to the search fallback),
`needs_user_choice` (the choice gate never fired) and every `risky_*` score
(risk gating fell back to the keyword floor alone).

Fixed in `validate_noul()`, which now reads `noul` first and keeps the previous
shapes as fallbacks. Confirmed live afterwards: `start_here` returns
`{'probability': 0.9, 'confidence': 0.8}`. Regression tests pin both the live
boolean and the live choice shape.

### Other corrections made

| Finding | Change |
|---|---|
| mercury-2.5 returns a flat `{"1": "Chennai"}`, not `{"values": {…}}` | `FIELD_VALUES` asks for the flat shape; the parser accepts either |
| Jev cold start is 4–5× warm | `core/http.warm()` is load-bearing; keep it |
| AssemblyAI bills idle connection time | Socket now opens on key down and idle-closes after 30 s (`stt.idle_close_s`) |
| …which would have lost the first second of speech | `press()` starts capture **before** the handshake; frames buffer in the mic queue |

### Also wired up (no API cost)

- **Profile → field cache.** `core/profile.py` reads `data/profile.yaml`, keeps
  only known keys, flattens the address, and merges into the Mercury payload
  **only** — never into an event, so it cannot reach the log or the status page.
- **`task_done` verifier.** Rides on the same request; a `DONE` the verifier
  disputes (p < 0.5) returns the run to `ready` instead of claiming success,
  capped at `MAX_DISPUTED_DONE` so a stubborn disagreement cannot loop.
- **Tab-closed recovery.** `return_to_opener()` finally has a caller: if an
  observe fails after an action, the agent falls back to the opener tab instead
  of stranding the run on a dead target.
- **Voice-layer tests** (`tests/test_voice_capture.py`, 8 tests) pinning the
  connection parameters, the keyterms limits, idle-open behaviour and the mic
  buffering.

## 12. Browser layer, verified live — 2026-09-26

Remote debugging enabled, Chrome wrote `DevToolsActivePort` (9222), the daemon
came up. Everything below ran against a real Chrome; the mutating tests ran in
**our own tab against the local `safety_fixture.html`**, never a real site.

### Read-only, on a real Google results page

| | |
|---|---|
| attach | 134 ms warm (11 s the first time — Chrome's one-time "Allow remote debugging?" prompt) |
| observe | 96 ms, then 45 ms |
| extraction | 25 elements with correct roles, labels and operations; the search box correctly offered both `TYPE_TEXT` and `CLICK` |
| freshness | 50 ms, `True` |
| keyword floor | no false positives on that page |

Note: browser-harness renames the controlled tab with a 🐴 prefix so the user can
see which tab it owns.

### Executor and guards, on the local fixture

| Check | Result |
|---|---|
| Type into an ordinary field | `Passenger name` → `'Melvin S'` |
| Type into OTP / Card / CVV / UPI PIN / Aadhaar | all refused by the executor |
| `type="password"` | never even offered as a fill action — `snapshot.js` excludes it at source |
| In-page JS guard, Python check bypassed | OTP and CVV still refused |
| Click a safe control | executed; the page recorded the touch |
| Freshness after a title change | `False` |
| Per-action guard after removing the target | `False`, and `act()` refused with `StalePage` |
| Click a button under a full-screen overlay | refused as covered |

### The bug this found: a closed tab

`Browser.__init__` only created a tab when `open_in="new_tab"` **and** a URL was
passed. With `url=None` it fell through to `attach()`, taking over the user's
active tab — and `close()` then closed it, because ownership was inferred from
*configuration* rather than tracked as *fact*. A real Google tab was closed
during testing.

Fixed: `open_in="new_tab"` always creates a tab, and `self.owned` records what
actually happened. `attach()` and `switch()` (following a page-opened tab) both
set it to `False`, so only a tab this object created can ever be closed. Two
regression tests pin it.

### End to end, one typed goal

`"type Chennai into the From field"` on the fixture, budget capped at 4 steps /
6 model calls:

```
     0ms  TranscriptFinal   "type Chennai into the From field"
    21ms  Normalized        facts {'cities': ['Chennai']}
   626ms  IntentClassified  new_task p=0.84 (596ms, jev-1.13.0)
  1599ms  StartResolved     search  google.com/search?q=travel+booking+site+From+field
  3438ms  ActionDecided     TYPE_TEXT Search p=1.00 (350ms)
  3496ms  RiskScored        0/25 risky (464ms)
  4235ms  ActionExecuted    fill Search = Chennai
  8715ms  ActionExecuted    navigate Go to makemytrip
 10084ms  RunStatusChanged  blocked  Reached the run's model-call budget
```

Every stage worked: intent, start resolution, navigation from a code-owned URL,
snapshot, decision, field cache, execution, re-observation, `NAVIGATE`, and a
clean stop at the budget. Risk scoring and the field cache both produced real
results, which is the second confirmation that the `noul` fix took.

The agent chose `search` rather than staying on the fixture — defensible, since
the page is titled "Safety fixture" and the goal reads like a UI instruction
rather than a task. No model wrote a URL: Mercury supplied query *words*, the
template supplied the URL.

Warm per-tick Jev latency held at **320–355 ms** across the whole run.

### What that run cost, and the fix it prompted

15 Jev requests and 5 Mercury requests for 3 actions — and **8 of the 15 Jev
calls were risk scoring**, several on fingerprints the agent had already moved
past. Pages settle through several fingerprints and only the last is ever acted
on, so scoring the intermediate ones is pure waste.

`RiskScorer.score()` and `FieldCache.prefetch()` now cancel work still running
for a superseded fingerprint (or an older `goal_version`). Three tests cover it.

### Still untested against a real page

Tab following (`openerId` adoption) and the choice gate on a genuine results
page — both need a site that opens a popup or returns many similar results.
