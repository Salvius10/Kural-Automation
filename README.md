# Kural — voice-enabled browser agent

Speak a goal. Your own Chrome does it, in about a second.

A local, single-user, **voice-input** browser agent built on
[`browser-use/jev-ultrafast`](https://github.com/browser-use/jev-ultrafast).
Hold a hotkey, speak, release: the agent drives the Chrome you already have
open. Voice is input only — there is no TTS. All feedback appears in the
browser and on a local status page at <http://127.0.0.1:8766>.

**[Plan.md](Plan.md) is the complete build brief** — architecture, latency
budget, safety rules, milestones and exit criteria. Read it before changing
anything here. [DECISIONS.md](DECISIONS.md) records choices made while
building, [ERRORS.md](ERRORS.md) every bug hit and how it was fixed, and
[agent/UPSTREAM.md](agent/UPSTREAM.md) every local change to the vendored agent
core.

## Setup

```bash
uv sync
cp .env.example .env              # fill in the three API keys
cp data/profile.example.yaml data/profile.yaml
uv run browser-harness --doctor   # connect to Chrome
uv run python main.py             # agent + status page
```

Then open <http://127.0.0.1:8766>, focus Chrome, hold **F9** and speak.
Without a microphone, `uv run python main.py --type` reads goals from stdin.

## Checks

```bash
uv run ruff check .
uv run pytest            # offline only; no paid API calls
```

## Where this is

Milestones from [Plan.md §14](Plan.md). Exit criteria that need live API keys
are marked *pending keys*.

| Milestone | State |
|---|---|
| M0 — baseline, vendored core | Vendored at upstream `1231850`, [agent/UPSTREAM.md](agent/UPSTREAM.md). Live baseline run *pending keys* |
| M1 — async core, events, logging | Done: `core/{interfaces,events,bus,timing,log,http}.py`, JSONL per session, `scripts/{replay,latency_report}.py` |
| M2 — session manager, typed input | Done: explicit state machine, fused intent+decision, corrections, cancel, `--type` CLI |
| M2a — dynamic start and navigation | Done: active-tab attach, `sites.yaml`, named/here/site/search resolution, `NAVIGATE`, tab following, choice gate, profile |
| M3 — speed layer | Field cache, background risk scoring and pre-warming are in; the p50 numbers are *pending keys* |
| M4 — gates and safety | Done: confirm/info/manual/choice gates, blocked fields, kill switch, budgets. Safety set green (`tests/test_safety_set.py`) |
| M5 — voice input | Hotkey, mic and the AssemblyAI v3 adapter are written against the verified API; recordings and the bake-off are *pending keys* |
| M6 — status page | Done: transcript, step, prompts, timing bar, history |

Offline suite: `uv run pytest` — 200 tests, no network (a fixture makes any API
call fail loudly).

### To run it live you need three keys in `.env`

| Key | Where from |
|---|---|
| `TYPESAFE_API_KEY` | <https://typesafe.ai> — decisions (Jev) |
| `INCEPTION_API_KEY` | <https://platform.inceptionlabs.ai> — field text (Mercury) |
| `ASSEMBLYAI_API_KEY` | <https://assemblyai.com> — speech to text |

Then: `uv run browser-harness --doctor` to connect Chrome, and
`uv run python evals/run_tasks.py` for the first real numbers.
