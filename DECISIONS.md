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
three hours, so the adapter reconnects with backoff (and sends `KeepAlive`
within a session — though see 13a: the socket no longer idles for long).
With `format_turns` on, the formatted transcript arrives as a *second*
`Turn`, so `end_utterance()` waits a 350 ms grace period for it after the raw
final. Revisit if AssemblyAI changes the default model (currently
`universal-3-5-pro`; we pin `universal-streaming-english`).

**2. TypeSafe `noul` response shape — verified 2026-09-26 against `jev-1.13.0`.**
The live shape is **`{"type": "noul", "noul": 0.9}`** — a bare probability under
`noul`, with *no* `confidence` field. Neither form the Plan implied. Our
validator rejected it, so every boolean head (`start_here`, `needs_user_choice`,
every `risky_*`) was being silently discarded: the defensive design held — an
unparsed boolean became "no answer", never a wrong "yes" — but start resolution,
risk scoring and the choice gate were all inert. `validate_noul()` now reads
`noul` first and keeps the other shapes as fallbacks, and synthesises confidence
as `|p − 0.5| × 2`. Regression test: `test_the_live_boolean_shape_parses`.

Choice heads match `validate_choice()` exactly, plus a harmless extra `"type":
"choice"` key. `jev-1.13.0` is a valid pinned id.

**3. Mercury on Inception — verified 2026-09-26.**
`GET /v1/models` returns `["mercury-2", "mercury-2.5"]`; `mercury-2.5` is
correct. `reasoning_effort: "instant"` is accepted and real
(`completion_tokens_details.reasoning_tokens: 0`). JSON mode works.

**3a. Field values come back as a flat mapping, not a `values` wrapper.**
mercury-2.5 returns `{"1": "Chennai", "2": null}`, not `{"values": {…}}`. Rather
than fight it, `FIELD_VALUES` now asks for the flat shape and the parser accepts
either, so a prompt or model change cannot silently empty the cache. `null` for
an unknown value was confirmed live on both the batch and single-field paths —
that is what opens the missing-info gate instead of inventing a passenger name.

**3b. Measured latency, 2026-09-26.** Jev cold: **1405 ms**. Jev warm: **299–345 ms**,
inside the ≤ 400 ms budget of Plan §4.1 — so `core/http.warm()` is load-bearing,
not a nicety. Fusing the intent and start heads onto the decision costs 529
extra input tokens (2620 vs 2091) and saves an entire round trip. Mercury runs
470–660 ms, which is fine for the background prefetch but is the real cost of a
cache **miss** on the critical path. AssemblyAI's handshake is ~980 ms.

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

**13a. The STT socket closes when idle.**
AssemblyAI bills streaming on connection duration, *idle time included* ("a
WebSocket open for 60 minutes with 30 minutes of audio sent is billed for 60
minutes"). Plan §4.2.1 says keep it warm, which would bill an all-day session
for a few minutes of speech — and on a free tier, burn the quota while doing
nothing. Push-to-talk makes that unnecessary: the socket opens on **key down**,
a whole utterance before the transcript is needed, and closes `stt.idle_close_s`
(30 s) after the last one, so a burst of commands still shares one connection.

The consequence is that capture must start *before* the handshake: `press()`
calls `mic.begin()` first and lets frames buffer in the mic queue (~4 s) while
the socket opens, otherwise a cold key-down loses the first second of speech.

**13b. The agent activates its tab instead of detecting the focused one.**
Plan §7.11.1 says to attach to "Chrome's currently focused tab". There is no
cheap way to find it: `Target.getTargets` exposes no focus flag, evaluating
`document.visibilityState` in every target costs ~2 s per *background* tab
(measured: 61 s across 28 tabs, because background tabs are throttled), and
`document.hasFocus()` lies once `Emulation.setFocusEmulationEnabled` is on --
which we enable ourselves.

So `current_tab()` (the tab the *daemon* last attached to, which persists
between runs) still chooses the tab, but the agent now calls
`Target.activateTarget` on attach, on navigate, and when following a
page-opened tab. The user always sees which tab the agent is using, rather than
watching a foreground tab while the work happens three tabs away. Config:
`navigation.activate` (default true).

This was a real failure, not a theoretical one: the first live voice run
navigated to MakeMyTrip correctly and looked like it had done nothing at all.

**13c. A dead CDP session is recovered into a fresh tab, and never replays an action.**
The session id from `switch_tab()` was cached for the life of the process, so a
tab closing underneath the agent surfaced as a raw
`-32001 Session with given id not found` out of `Page.navigate` — which is
exactly how the first real voice run died. `Browser.call()` now detects a dead
session (several CDP wordings) and re-attaches once.

Two rules shape the recovery:

- **It opens a new tab rather than falling back to `attach()`.** Adopting
  whichever tab the user happens to have open would navigate their reading away
  to the agent's next URL. Observed in testing: recovery hijacked a GitHub tab
  and sent it to example.org. A fresh tab is `owned`, so it is also cleaned up.
- **A mutation is never replayed.** `observe()` and `call()` may retry after
  re-attaching; `act()` re-attaches and then raises `StalePage`, so the agent
  observes and decides again instead of guessing whether the click landed. This
  keeps upstream's "never retry a browser mutation" rule intact.

`attach()` also now verifies that the tab `current_tab()` names still appears in
`list_tabs()` — the daemon remembers a target across runs, and it may be closed.

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
