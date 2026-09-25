# Errors

Every bug hit while building this, what caused it, and how it was fixed. Kept
because most of them are the kind that come back — and because several were
found only by running the thing against a real browser and real APIs, not by
reading code.

Companion files: [Plan.md](Plan.md) is the spec, [DECISIONS.md](DECISIONS.md)
records design choices, [Implementation.md](Implementation.md) records what was
built and verified.

**Index**

- [A. Errors that reached the user](#a-errors-that-reached-the-user) — 4
- [B. Vendor contract mismatches](#b-vendor-contract-mismatches) — 3
- [C. Safety bugs caught by tests](#c-safety-bugs-caught-by-tests) — 4
- [D. Robustness and lifecycle bugs](#d-robustness-and-lifecycle-bugs) — 5
- [E. Cost and waste](#e-cost-and-waste) — 2
- [F. Bugs in the tests and probes themselves](#f-bugs-in-the-tests-and-probes-themselves) — 5
- [Patterns worth remembering](#patterns-worth-remembering)

---

## A. Errors that reached the user

### A1. `-32001 Session with given id not found` — the first real voice run crashed

**Symptom.** The first voice command ever spoken produced a traceback instead of
a browser action:

```
RuntimeError: {'code': -32001, 'message': 'Session with given id not found.'}
  main.py:104 release -> session.utterance -> route -> dispatch -> new_task
  -> browser.navigate -> Browser.navigate -> self.call("Page.navigate")
```

**Cause.** `Browser` captured a CDP session id from `switch_tab()` at startup and
cached it for the life of the process, with no revalidation. Between the app
attaching and the user speaking, the tab that session pointed at was closed — in
this case an orphaned test tab left over from an earlier probe (see F3). The
cached session then referred to nothing.

Made worse by `attach()` trusting `current_tab()`, which returns the tab the
*daemon* last attached to. The daemon remembers a target across runs, so it can
name a tab that no longer exists.

**Fix.** Three changes in `agent/browser.py`:

1. `attach()` verifies the remembered target still appears in `list_tabs()`
   before adopting it.
2. `call()` detects a dead session — CDP reports it in several wordings, all
   collected in `DEAD_SESSION` — re-attaches once, and retries.
3. `act()` re-attaches but **raises `StalePage` instead of retrying**, so the
   agent observes and decides again rather than guessing whether a click landed.
   Upstream's "never retry a browser mutation" rule stays intact.

**Verified** by closing the agent's tab out from under it mid-run: `navigate`
recovered and continued.

**Guards.** `test_a_dead_remembered_tab_is_not_used`,
`test_a_dead_session_is_recovered_once`,
`test_a_dead_session_mid_action_never_replays_the_action`.

---

### A2. "It doesn't open the site" — the agent worked in a tab nobody could see

**Symptom.** A voice command ran end to end — transcript correct, intent
`new_task` at 0.97, `StartResolved: named → makemytrip`, status page showing
`https://www.makemytrip.com/` and `DONE` — and the browser appeared to do
nothing at all.

**Cause.** The agent navigated a tab it never brought to the front. With ~28 tabs
open, the work happened somewhere invisible. The tab it chose came from
`current_tab()`, which is the daemon's remembered target, not the tab the user
is looking at.

Trying to detect Chrome's genuinely focused tab turned out to be a dead end,
which is worth recording:

- `Target.getTargets` exposes no focus flag.
- Evaluating `document.visibilityState` in every target costs **~2 s per
  background tab** — 61 s across 28 tabs, because Chrome throttles background
  tabs.
- `document.hasFocus()` **lies** once `Emulation.setFocusEmulationEnabled` is on,
  which the agent enables itself. In the measurement, two of four "focused" tabs
  were orphaned test tabs.

**Fix.** Stop guessing which tab the user is watching; make the agent's tab the
one they watch. `Browser.show()` calls `Target.activateTarget` on attach, on
navigate, and when following a page-opened tab. Config `navigation.activate`
(default true).

**Not a bug, same report.** The same command "did nothing else" because the goal
was *"open make my trip"* — navigating satisfies it completely, so `DONE` was
correct. Work has to be in the goal: *"…and search flights from Chennai to
Mumbai on 2nd October"*.

---

### A3. The agent closed one of the user's tabs

**Symptom.** A Google search tab the user had open ("conglomerates meaning")
disappeared during executor testing.

**Cause.** `Browser.__init__` only created a tab when `open_in="new_tab"` **and**
a URL was passed:

```python
if url is not None and open_in == "new_tab":   # url was None
    self.open_tab(url)
else:
    self.attach()                              # took the user's active tab
```

With `url=None` it fell through to `attach()`. Then `close()` closed that tab,
because ownership was inferred from **configuration** (`open_in == "new_tab"`)
rather than from what actually happened.

**Fix.** `open_in="new_tab"` always creates a tab. Ownership became a recorded
fact, `self.owned`: set `True` only in `open_tab()`, and `False` in `attach()`
and in `switch()` (a tab the page opened is not ours). `close()` checks
`self.owned`, never the config.

**Guards.** `test_only_a_tab_we_created_is_ever_closed`,
`test_following_a_page_opened_tab_does_not_make_it_ours`.

---

### A4. Recovery hijacked a tab the user was reading

**Symptom.** While testing the A1 fix, the agent's recovery path navigated the
user's `github.com/yibie/awesome-jev` tab to `example.org`.

**Cause.** The first version of `reattach()` called `attach()`, which falls back
to "any usable tab". That adopts a tab the user is reading and then navigates it
to wherever the agent is going next.

**Fix.** `reattach()` opens a **fresh** tab instead. The previous page is gone
either way, and a new tab destroys nothing; being `owned`, it is also cleaned
up afterwards. `attach()` keeps its fallback for *startup*, where using the
current tab is the entire point.

The hijacked tab was restored with `history.back()`.

---

## B. Vendor contract mismatches

These were all found by calling the live APIs. None were visible from the docs
or from reading code.

### B1. Every boolean answer from Jev was being silently discarded

**Symptom.** None — and that is what made it dangerous. Everything appeared to
work; three features simply never did anything.

**Cause.** Boolean (`noul`) answers come back as:

```json
{"type": "noul", "noul": 0.9}
```

A bare probability under `noul`, with **no** `confidence` field. `validate_noul()`
expected `probability`, or a yes/no `probabilities` map — neither of which
exists. Every boolean head therefore failed validation.

The blast radius was large and completely silent:

| Head | Consequence of always failing |
|---|---|
| `start_here` | Every task fell through to the web-search fallback |
| `needs_user_choice` | The choice gate never fired |
| `risky_<index>` | Risk gating fell back to the keyword floor alone |

**Why nothing unsafe happened.** The validator was written to treat an
unparsable boolean as *no answer*, never as "yes" ([DECISIONS.md §2](DECISIONS.md)).
That design choice is the only reason a total validation failure degraded into
lost features instead of unapproved clicks.

**Fix.** `validate_noul()` reads `noul` first, keeps the other shapes as
fallbacks so a future change cannot silently disable everything again, and
synthesises the missing confidence as `|p − 0.5| × 2`. Confirmed live
afterwards: `start_here` → `{'probability': 0.9, 'confidence': 0.8}`.

**Guards.** `test_the_live_boolean_shape_parses`,
`test_alternative_boolean_shapes_still_parse`, `test_malformed_booleans_are_rejected`.

---

### B2. Mercury returns a flat mapping, not the wrapper we asked for

**Symptom.** `MercuryError: Text model returned no values object; cache left empty.`
The field-value cache was empty on every page.

**Cause.** The prompt asked for `{"values": {"1": "Chennai"}}`. mercury-2.5
returns the obvious thing instead:

```json
{"1": "Chennai", "2": "Mumbai", "3": null}
```

JSON mode is not a strict schema, so nothing enforced the wrapper.

**Fix.** Match the model rather than fight it: `FIELD_VALUES` now asks for the
flat shape, and the parser accepts **either** — so a future prompt tweak or
model change cannot silently empty the cache again. Key and value validation is
unchanged: only requested indexes, strings under 2,000 chars.

---

### B3. Streaming is billed on connection time, including idle

**Symptom.** No error — a cost bug, found while reading the pricing docs.

**Cause.** Plan §4.2.1 says to hold the AssemblyAI WebSocket open for latency,
and the adapter did. But AssemblyAI bills *connection duration*: "a WebSocket
open for 60 minutes with 30 minutes of audio sent is billed for 60 minutes."
An all-day agent would bill all day for a few minutes of speech — and on a free
tier, burn the quota while doing nothing.

**Fix.** Push-to-talk makes the warm socket unnecessary: **key down** happens a
whole utterance before the transcript is needed, so the handshake hides inside
the speech. The socket now opens on key down and closes `stt.idle_close_s`
(30 s) after the last utterance, so a burst of commands still shares one
connection.

**This fix caused D5** — see below.

---

## C. Safety bugs caught by tests

### C1. The blocked-field list existed twice, and had already drifted

**Symptom.** `test_blocked_fields_are_recognised[Security code]` failed:
`blocked_label("Security code")` was `True` in the executor but `False` in the
field cache.

**Cause.** Two independent lists — `BLOCKED_LABELS` in `agent/browser.py` and
`BLOCKED_HINTS` in `core/field_cache.py`. They had already diverged. A field
could be refused by one layer and typed by another.

**Fix.** One list in `core/sensitive.py`, imported by the executor, the in-page
JS guard and the cache. `js_pattern()` hands the *same* regex to the browser, so
Python and JavaScript cannot drift either.

---

### C2. "Pincode" was treated as a password field

**Symptom.** `test_ordinary_fields_are_not_blocked[Pincode]` failed.

**Cause.** Substring matching. `"pincode".contains("pin")` is `True`, so every
address form would stall on a "please type this yourself" gate.

**Fix.** Whole-word matching via one regex with lookarounds:
`(?<![a-z0-9])(?:password|otp|pin|cvv|…)(?![a-z0-9])`. "PIN" is blocked,
"Pincode" and "Shipping" are not.

---

### C3. A missing field value crashed the tick instead of asking the user

**Symptom.** With no cached value, the agent hit
`MercuryError: Text helper returned no valid field value` and the tick failed.

**Cause.** Plan §7.6 says a `null` value means the goal lacks that information →
open the missing-info gate. That was implemented for the *cached* path only. On
the synchronous fallback, `single_field_value()` raised for a genuine `null`
exactly as it did for malformed output — upstream's behaviour, because upstream
had no gate to open.

**Fix.** `single_field_value()` returns `None` for a real null and raises only
on malformed output. `Agent.execute()` turns `None` into the same
`GateRequired("info", …)` the cached path produces. Both paths now ask instead
of failing — and neither ever invents a value.

**Guards.** `test_a_missing_field_value_opens_an_info_gate`,
`test_a_cached_null_opens_the_gate_without_a_model_call`.

---

### C4. `NAVIGATE_TARGET` instructions were written but never sent

**Symptom.** None. Found while auditing for unused constants.

**Cause.** `build_questions()` applied `[NEXT_ACTION, TARGET]` to every target
head, including `navigate_target`. `TARGET` is about picking an element on the
page, so the navigate head was being asked the wrong question.

**Fix.** `rules = [NEXT_ACTION, NAVIGATE_TARGET] if operation == "NAVIGATE" else
[NEXT_ACTION, TARGET]`.

---

## D. Robustness and lifecycle bugs

### D1. `FieldCache.prune()` crashed on every real page

**Symptom.** `KeyError: 'node'` in `core/field_cache.py`.

**Cause.** `prune()` read `action["node"]` for every action, but control
actions — `wait`, `scroll_up`, `scroll_down` — have no node. Every real page has
at least a `wait`, so this would have fired constantly.

**Fix.** `{action["node"] for action in page.get("actions", []) if "node" in action}`.

---

### D2. Cancellation was being swallowed

**Symptom.** None observed; found by reading.

**Cause.** `except (MercuryError, asyncio.CancelledError)` and
`except (asyncio.CancelledError, Exception)`. `CancelledError` is a
`BaseException` in Python 3.12 precisely so it is *not* caught by
`except Exception` — listing it explicitly defeats that, so cancelling a
background prefetch or risk score left the task half-dead.

**Fix.** `except asyncio.CancelledError: … raise` first, then swallow only real
failures.

---

### D3. A busy status port killed the whole agent

**Symptom.** `SystemExit: 3` on startup when port 8766 was taken.

**Cause.** uvicorn calls `sys.exit(STARTUP_FAILURE)` when its bind fails. A
stray process holding the port would take down the agent along with the status
page.

**Fix.** `ui.server.serve()` binds the socket itself and hands it to uvicorn via
`serve(sockets=...)`. `main.py` catches `OSError` and degrades to "running
without the status page".

---

### D4. The status page WebSocket blocked shutdown forever

**Symptom.** Ctrl-C hung; the server never exited.

**Cause.** The handler sat in `await queue.get()` waiting for the next event.
Starlette only surfaces a disconnect on a `receive()`, which the handler never
called — so it never learned the page had closed, and uvicorn waited for it.

**Fix.** Race a `receive()` task against the queue with
`asyncio.wait(..., FIRST_COMPLETED)`; whichever finishes first ends the handler.

---

### D5. The idle-close fix would have eaten the first second of every command

**Symptom.** None yet — caught by reasoning about the measured 980 ms handshake
immediately after implementing B3.

**Cause.** `press()` awaited `stt.begin_utterance()` (which now opens the socket)
**before** `mic.begin()`. With the socket open that was free; with idle-close it
meant roughly a second of speech was never captured.

**Fix.** Reorder: `mic.begin()` first, then connect. Frames buffer in the mic
queue (~4 s of capacity) and the pump drains them in order once the socket is
up.

**Guard.** `test_the_mic_buffers_frames_while_the_socket_opens`.

**Lesson.** A performance fix moved work onto the critical path in a place the
fix itself never looked at.

---

## E. Cost and waste

### E1. Over half of all Jev calls were scoring pages the agent had left

**Symptom.** A 10-second run with 3 actions cost **15 Jev requests** and 5
Mercury requests. Eight of the fifteen were risk scoring, several on the same
page moments apart (`0/25 risky`, then `0/27 risky` 120 ms later).

**Cause.** A page settles through several fingerprints as it loads. Every new
fingerprint triggered a fresh background risk score and field prefetch, and
nothing cancelled the earlier ones — whose results could never be used, because
only the final fingerprint is ever acted on.

**Fix.** `RiskScorer.score()` and `FieldCache.prefetch()` now call `supersede()`,
cancelling in-flight work for any older fingerprint (or older `goal_version`).

**Guards.** `test_a_superseded_risk_score_is_cancelled`,
`test_a_superseded_field_prefetch_is_cancelled`,
`test_a_goal_change_supersedes_an_in_flight_prefetch`.

---

### E2. The offline test suite was calling paid APIs

**Symptom.** `pytest` took 4.4 s. It should be instant — nothing in `tests/`
should touch a network.

**Cause.** Routing tests exercised a CLICK, which reaches
`RiskScorer.is_risky()`. With no cached score it falls back to **one synchronous
Jev call** — by design on the critical path, but not something a test should do.
The calls failed on a dummy key and were swallowed, so nothing looked wrong
beyond the clock.

**Fix.** An autouse fixture in `tests/conftest.py` stubs `agent.model.post_json`
and `core.mercury.complete_json` to raise `NetworkUsed`. Callers that treat a
model failure as "no answer" keep working — which is the correct offline
behaviour to test — and anything that would *act* on a model answer fails
loudly. Suite dropped to 1.5 s.

---

## F. Bugs in the tests and probes themselves

Recorded because each one briefly produced a *wrong conclusion about the
product*, which is worth more than the bug.

### F1. Tests cancelled the run loop before it ever ran

`await session.stop_loop()` right after `utterance()` cancelled the loop task
before the event loop had scheduled it. Several loop tests were silently
no-ops — including the one that was supposed to prove the choice gate fires.

**Fix.** `drain(session)` awaits genuine completion (the scripted `choose`
returns `DONE` when the queue empties) and only then cleans up.

### F2. A test asserted the wrong thing, and "found" a bug that did not exist

`test_malformed_choices_are_rejected` built its `ids` set from the answer under
test, so the "choice not offered" case accidentally offered it. The *test* was
wrong, not `validate_choice`. Fixed by passing `ids` explicitly per case.

### F3. A crashed probe left orphan tabs in the user's browser

`probe_fresh.py` raised `StopIteration` before reaching `close()`, leaving two
local-fixture tabs open. One of them is the most likely cause of **A1**: a
leftover tab the daemon later pointed at, which then vanished.

**Lesson.** A probe that mutates browser state needs `try/finally`, exactly like
production code.

### F4. The freshness guard looked broken and was not

A probe appended `<p>changed</p>` to the body and expected `fresh()` to return
`False`. It returned `True`, which looked like a serious guard failure. In fact
the element landed **below the fold**, and the marker deliberately tracks only
*visible* semantics.

Re-tested properly — title change, element removal, full-screen overlay — all
three behaved correctly.

**Lesson.** Before reporting a guard as broken, check the test exercises what the
guard actually claims to cover.

### F5. Two probes deadlocked themselves

- The UI smoke test called blocking `urllib.request.urlopen` **on the same event
  loop** as the server it was testing, so the server could never answer.
  Fixed with `httpx.AsyncClient`.
- A heredoc-written Python file silently failed to write, and the "fix" was
  applied to a file that still held the old contents. Switched to the Write tool
  for large files.

Console encoding also bit twice: Windows `cp1252` stdout cannot print `₹` or the
🐴 emoji browser-harness adds to tab titles. `PYTHONIOENCODING=utf-8` for probes.

---

## Patterns worth remembering

**Silent failure is the expensive kind.** B1 caused no error, no log line and no
visible symptom — three features simply did nothing for as long as the code
existed. It was found only by printing a raw API response. Anything that
validates an external contract should be exercised against the real thing early,
and the raw payload logged once.

**Fail safe, and a contract bug degrades instead of exploding.** The single
reason B1 was a feature outage rather than an unapproved payment is that
`validate_noul()` treated an unparsable answer as *no*. Choosing the safe
direction for "I don't understand this" paid for itself.

**Never infer state you can record.** A3 (ownership from config) and A1 (a
session id assumed valid forever) are the same mistake: deriving a fact from an
assumption instead of tracking what actually happened.

**A fix is a change, and changes have their own bugs.** B3 → D5, and A1 → A4.
Both fixes were correct and both introduced a new problem in code the fix never
touched. Re-check the surrounding path after fixing something.

**Local verification is not verification.** Every bug in section A survived a
green 200-test suite. They needed a real browser, a real API key, and a real
person pressing a key.
