# Upstream: browser-use/jev-ultrafast

Vendored from <https://github.com/browser-use/jev-ultrafast>.

- Commit: `1231850a0bf1a0c0341fe408ef1668dbbfdfac46` ("docs: announce the Cloud
  waitlist below the README title (#30)", 2026-09-18)
- Upstream licence: MIT (see `LICENSE-jev-ultrafast` at the repo root)
- Files taken: `agent.py`, `browser.py`, `model.py`, `questions.py`, `snapshot.js`
- Files not taken: `demo.py` (replaced by `ui/server.py`), `static/*`, `examples/`,
  `scripts/`

Upstream's safety properties are load-bearing and are **not** weakened by any
change below: model output only ever selects an index from a code-built list;
every target resolves to an observed DOM node; freshness is checked before
acting and again immediately before input; occluded controls are rejected;
invalid model output executes nothing; a decision is consumed before acting.

## Local changes

Each entry says what changed and why. Keep this list current — it is the diff
review surface for the vendored code.

### `questions.py`
1. **Added `NAVIGATE` to the operation vocabulary** and its rules (Plan §7.11.4).
   Upstream always starts at a developer-supplied URL and cannot leave the site.
2. **Added instruction blocks** for the new question heads: `INTENT`,
   `START_HERE`, `START_SITE`, `NAVIGATE_TARGET`, `NEEDS_USER_CHOICE`, `RISKY`,
   `TASK_DONE`, and `FIELD_VALUES` (batched field text). (Plan §10)
3. `MAX_STEPS` is superseded by `budgets.max_steps` in `config.yaml` (default
   40, upstream 60). The constant stays in the file as documentation of the
   upstream default; `Agent` reads the config value.

### `model.py`
1. **Async.** `post_json`/`choose`/`field_text` are `async` and use the shared
   HTTP/2 clients in `core/http.py` instead of a module-level `httpx.Client`.
   Retry/backoff semantics are preserved.
2. **`action_space()` gained `NAVIGATE`** as an operation with a
   `navigate_target` head built from `data/sites.yaml` (code-owned URLs only —
   the model picks a site id, never writes a URL).
3. **`choose()` accepts `extra_questions`** so the session manager can fuse
   intent/start/choice-gate heads into the same request (Plan §4.2.5), and
   returns their validated answers alongside the action decision.
4. **`field_text()` → Inception.** Upstream's OpenRouter/DeepSeek `reasoning`
   parameter is replaced by Inception's `reasoning_effort`; the key, base URL
   and model come from `INCEPTION_*` / `MERCURY_*`. Lives in `core/mercury.py`;
   `model.py` keeps a thin wrapper so the upstream call site is recognisable.
5. `validate_choice()` is unchanged and is now also used for the new heads;
   `validate_noul()` was added for boolean (`noul`) questions.
6. Page text and history caps read from config (`page_text_chars`,
   `history_actions`) instead of the hardcoded 6000/10.

### `browser.py`
1. **Async facade.** The synchronous Browser Harness/CDP calls run on one
   dedicated worker thread (`AsyncBrowser`), so ordering is preserved and the
   event loop never blocks. The synchronous `Browser` body is upstream's.
2. **No URL at construction.** `Browser.attach()` attaches to Chrome's active
   tab (Plan §7.11.1); `Browser.navigate(url)` is separate. Upstream's
   `Target.createTarget` path is kept for `navigation.open_in: new_tab`.
3. **Blocked-field guard** before any `fill` execution (Plan §7.9): password,
   OTP, CVV, card and similar fields are refused in the executor, not just in
   the policy.
4. **Tab following**: new targets whose `openerId` is the attached tab are
   adopted (Plan §7.11.5); other tabs are never touched.

### `agent.py`
1. **Async generator loop**; `tick()` is awaited, `run()` yields after each tick.
2. `set_goal(text, version)` applies between ticks only (Plan §7.8.2).
3. Event callback emits `ActionDecided` / `ActionExecuted` / `PageObserved` /
   `RunStatusChanged` with timings (Plan §9).
4. Gates: a CLICK consults the choice gate then the risk scorer, a TYPE_TEXT
   consults the blocked-field list then the field cache; any of them returns
   `GateRequired` instead of acting (Plan §7.6, §7.7, §7.9, §7.11.6). The
   `needs_user_choice` head rides on ticks where the page is result-like, so
   the gate costs no extra round trip.
5. `NAVIGATE` is executed by the browser, not by a model-written URL.
6. Screenshots are off by default — upstream defaulted them on, and they are
   not allowed in the latency path (Plan §4.3).
7. A `null` field value opens the missing-info gate on both the cached and the
   synchronous path; upstream raised in both cases because it had no gate.

### `snapshot.js`
Unchanged.
