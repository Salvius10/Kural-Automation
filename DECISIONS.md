# Decisions

Plan.md §17.10: where something was ambiguous, prefer the simpler design and
write the decision down. Each entry says what was ambiguous, what was chosen,
and what would change the answer.

## Vendor APIs

**1. AssemblyAI v3 streaming parameters — verified 2026-09-25.**
Checked against the streaming API reference before implementing (Plan §17.2):
`wss://streaming.assemblyai.com/v3/ws`; the API key goes in the `Authorization`
header with **no** `Bearer` prefix; `encoding=pcm_s16le` and `sample_rate=16000`
are the defaults we want; `format_turns` and `end_of_turn_confidence_threshold`
exist for Universal Streaming only; the silence parameter is `max_turn_silence`
(ms), so `config.yaml`'s `max_turn_silence_ms` maps onto it; `keyterms_prompt`
is a **JSON-encoded array** in the query string, max 100 terms of ≤ 50 chars;
client messages are `ForceEndpoint`, `Terminate`, `KeepAlive` and
`UpdateConfiguration`; `Turn` carries `transcript`, `end_of_turn`,
`turn_is_formatted`, and `utterance` at turn end. Sessions auto-close after
three hours, so the adapter reconnects with backoff and sends `KeepAlive` while
idle. With `format_turns` on, the formatted transcript arrives as a *second*
`Turn`, so `end_utterance()` waits a 350 ms grace period for it after the raw
final. Revisit if AssemblyAI changes the default model (currently
`universal-3-5-pro`; we pin `universal-streaming-english`).

**2. TypeSafe `noul` response shape — not yet verified against a live key.**
The Plan describes boolean questions as returning "the probability of yes" but
not the field name. `validate_noul()` accepts either a bare `probability` or a
`probabilities` map with a `yes`/`true` key, and treats anything else as *no
answer* — never as yes. This is the safe direction: an unparsable risk score
falls back to the keyword floor rather than approving a click. Verify against
the TypeSafe docs on first live run and tighten to the real shape.

**3. Mercury on Inception — not yet verified against a live key.**
`reasoning_effort: "instant"`, `response_format: {"type": "json_object"}`,
`temperature: 0`. Every response is validated in code because JSON mode is not a
strict schema. Confirm the model id with `GET /v1/models` before M3.

## Behaviour

**4. "Next Friday" resolves to the next occurrence strictly in the future.**
dateparser returned `None` for "next friday" under our settings, and the phrase
is genuinely ambiguous. Weekdays are now resolved by our own rule: bare, "this",
"coming" and "next" all mean the next occurrence 1–7 days ahead (so on a Friday,
"Friday" means a week later). It is one rule, statable in a sentence, and a
wrong reading costs one spoken correction. Change it only if the voice eval set
shows people mean the week after.

**5. The first action runs inside the awaited routing path.**
Plan §7.5 says a `new_task` that starts on the current page executes the
returned decision immediately. That happens in `Session.new_task`, not in the
background loop, so the key-release → first-action budget covers it and is
measurable in one place.

**6. `intent_min_confidence` also gates the fused first action.**
Plan §7.5 says to execute the fused decision "when confidence ≥ threshold" but
names no separate threshold, and `config.yaml` (§15) has none. Rather than add a
key, the intent threshold is reused. A below-threshold decision is not thrown
away — the loop simply re-predicts on the next tick.

**7. One `Agent` per task; browser, field cache and risk scorer are shared.**
A new task gets a fresh history and a fresh step budget, which is what
`MAX_STEPS` is for, while the caches that are expensive to rebuild survive.

**8. A gate that times out cancels; it never approves.**
Plan §7.7 sets a 60 s timeout and §11.3 forbids auto-approval. So the timeout
resolves the gate as `timeout` and cancels the run.

**9. The sensitive-field vocabulary lives in `core/sensitive.py`.**
It was duplicated in the executor and the field cache, and the two lists had
already drifted ("security code" was in one and not the other). One list, one
regex, imported by the executor, the in-page JS guard and the cache. Matching is
whole-word, because substring matching blocked "Pincode" (contains "pin").

**10. A `null` field value is an answer, not an error.**
`single_field_value()` returns `None` when the model reports that the goal lacks
a value, and raises only on malformed output. Both the cached and the
synchronous path therefore open the same missing-info gate instead of failing
the tick. Upstream raised in both cases because it had no gate to open.

**11. New tabs are detected by polling `Target.getTargets` for `openerId`.**
Browser Harness's `drain_events()` only carries events for domains the daemon
has enabled, so `Target.targetCreated` is not guaranteed. One `getTargets` call
after an action is cheap and gives the same guarantee the Plan asks for: adopt
only tabs the attached page opened, never the user's other tabs.

**12. `Emulation.setDeviceMetricsOverride` is applied only to tabs we open.**
Upstream always owns its tab, so it can force a 1120×780 viewport. Attaching to
the user's visible tab and resizing it would be rude and visible; the user's tab
already has a viewport. Focus emulation is still enabled either way, so menus
and animations keep rendering when the tab is not frontmost.

**13. The status-page port is bound before uvicorn starts.**
uvicorn calls `sys.exit(3)` if its bind fails, which would take the agent down
because the status page was busy. `ui.server.serve()` binds the socket itself
and hands it over, so a busy port degrades to "running without the status page".

## Platform

**14. Windows: no uvloop.**
uvloop does not support Windows, so the dependency carries a
`sys_platform != 'win32'` marker and `main.py` installs it only if importable.
This is a latency cost on this laptop; measure it before chasing anything else
in Plan §4.2.

**15. Browser Harness calls run on one dedicated worker thread.**
Its `cdp()` helper is synchronous. `AsyncBrowser` funnels every call through a
single-worker executor: the loop never blocks, and CDP ordering is preserved
(which a thread pool of more than one would not guarantee).
