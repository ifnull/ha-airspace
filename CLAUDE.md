# CLAUDE.md

Project-specific guidance for Claude Code working on `adsb-enrich`.

> Always read `DESIGN.md` first — it's the source of truth for architecture, scope, and phase boundaries. This file covers conventions and workflow.

---

## Project at a glance

`adsb-enrich` is a Python service that consumes `aircraft.json` from one or more ADS-B receivers (dump1090, readsb, dump978-fa), enriches with reference databases, applies tagging/alert rules, and publishes to MQTT for Home Assistant consumption.

Distribution targets: HA add-on (primary), Docker image, pip package — same codebase.

**Non-goals to remember:** not a feeder, not a replacement for dump1090, not a website, not a HACS integration. If a change pushes toward any of those, stop and check in.

---

## Tech stack

- **Python 3.12+**. Use modern syntax (`match`, `|` unions, `Self`, parameterized generics without `typing.`).
- **Async-first.** `asyncio` throughout. Receivers, MQTT, DB refresh — all async. No threading except for CPU-bound DB parsing in an executor.
- **`httpx`** for HTTP (async, supports HTTP/2, sane timeouts).
- **`aiomqtt`** for MQTT (asyncio-native paho wrapper).
- **`pydantic` v2** for config schema validation. Strict mode; reject unknown fields with helpful errors.
- **`structlog`** for logging. JSON output in production, console renderer in dev.
- **`uv`** for dependency management and virtualenvs. `pyproject.toml` is canonical; no `requirements.txt`.
- **`ruff`** for lint + format (replaces black, isort, flake8).
- **`pytest`** + **`pytest-asyncio`** for tests.
- **`mypy --strict`** for type checking.

Don't introduce new dependencies without flagging it. The runtime footprint matters — this ships as a HA add-on and people run it on Raspberry Pis.

---

## Code conventions

### Style

- Format and lint with `ruff`. The repo's `pyproject.toml` is the source of truth — don't override locally.
- Line length: 100. Not 80, not 120.
- Type hints on every function signature, including tests. `Any` is a code smell; if you reach for it, leave a comment explaining why.
- `from __future__ import annotations` at the top of every module. Forward references everywhere, no exceptions.
- Imports: stdlib, third-party, first-party — three groups separated by blank lines. `ruff` enforces this.
- Prefer `dataclasses` (frozen where possible) over plain classes for data containers. Use `pydantic.BaseModel` only at config boundaries where validation matters.

### Naming

- Modules: `snake_case`, short, no plurals (`receiver.py` not `receivers.py`).
- Classes: `PascalCase`. Abstract base classes don't need an `Abstract` prefix; the abstract methods make it obvious.
- Functions/methods: `snake_case`, verb-first (`fetch_aircraft`, not `aircraft_fetch`).
- Constants: `SCREAMING_SNAKE_CASE` at module level. No `Constants` class.
- Private: single leading underscore. Don't use double-underscore mangling.

### Async patterns

- Every entry point is `async def`. The only `def` functions are pure utilities.
- Use `asyncio.TaskGroup` (3.11+) for structured concurrency, not `asyncio.gather` with bare exception handling.
- Cancellation must be respected — never swallow `asyncio.CancelledError`.
- HTTP clients are long-lived (`httpx.AsyncClient` per receiver, reused across polls). Don't open a new client per request.
- Backoff with `tenacity` or hand-rolled exponential — but always cap retries and surface failures via the receiver's `health()` rather than crashing the loop.

### Error handling

- **Receivers fail in isolation.** A flaky receiver should never take down the merger. Catch broadly inside the receiver's poll loop, log, mark unhealthy, yield empty observations, keep going.
- **MQTT disconnects are recoverable.** Reconnect with backoff. Don't drop aircraft state on disconnect; republish on reconnect.
- **Config errors fail fast.** Bad config → exit non-zero with a clear message at startup. Don't try to "do your best."
- **Use exceptions for unexpected, return values for expected.** A receiver returning empty list is expected (transient outage). A receiver returning malformed JSON repeatedly is unexpected (raise → caught at receiver boundary → marked unhealthy).

### Logging

```python
import structlog
log = structlog.get_logger()

log.info("aircraft_observed", hex=obs.hex, receiver=obs.seen_by, distance_nm=dist)
log.warning("receiver_unhealthy", receiver=name, error=str(e), retry_in_s=backoff)
```

- Event names are `snake_case` past-tense or descriptive nouns. Not English sentences.
- Structured fields, not f-string interpolation. Makes log aggregation actually useful.
- Use `log.exception(...)` only when re-raising or terminating; otherwise log the error message in a field.

---

## Repo layout

```
adsb-enrich/
├── DESIGN.md                  # architecture spec (READ FIRST)
├── CLAUDE.md                  # this file
├── README.md                  # user-facing
├── pyproject.toml
├── uv.lock
├── src/
│   └── adsb_enrich/
│       ├── __init__.py
│       ├── __main__.py        # entry point
│       ├── config.py          # pydantic schema + loading
│       ├── models.py          # AircraftObservation, AircraftState, etc.
│       ├── receivers/
│       │   ├── __init__.py
│       │   ├── base.py        # ReceiverSource ABC
│       │   ├── http.py        # HttpJsonReceiver
│       │   └── file.py        # FileReceiver (testing)
│       ├── databases/
│       │   ├── __init__.py
│       │   ├── loader.py      # async refresh logic
│       │   ├── mictronics.py  # CSV parser
│       │   └── adsbexchange.py
│       ├── merger.py          # multi-source AircraftState management
│       ├── enrichment.py      # flag + alert rule evaluation
│       ├── mqtt/
│       │   ├── __init__.py
│       │   ├── client.py      # connection management
│       │   ├── discovery.py   # HA MQTT discovery payloads
│       │   └── publisher.py   # topic routing + retention
│       └── geo.py             # haversine, bearing
├── tests/
│   ├── conftest.py
│   ├── fixtures/              # captured aircraft.json samples
│   ├── test_receivers.py
│   ├── test_merger.py
│   ├── test_enrichment.py
│   └── ...
└── addon/                     # HA add-on wrapper (added in Phase 4)
```

Add modules as needed; don't pre-create empty files.

---

## Testing

- **Test the abstractions, not the integrations.** `ReceiverSource` has a `FileReceiver` that replays captured JSON. Use it everywhere; don't mock `httpx`.
- **Capture real `aircraft.json` samples.** `tests/fixtures/` should have real data including edge cases (military aircraft, emergency squawks, on-ground aircraft, missing position, multiple receivers seeing the same hex).
- **Async tests use `pytest-asyncio`.** `@pytest.mark.asyncio` on test functions. Use the `auto` mode in `pyproject.toml` so we don't have to mark every test.
- **Time is a fixture.** Don't use real time in tests. Use `freezegun` or a `Clock` protocol passed into components.
- **No network in tests.** Ever. If a test needs network, it's an integration test and lives in `tests/integration/` with a marker that's skipped by default.
- **Coverage target: 80%** on `src/adsb_enrich/`. Don't chase 100%; the last 20% is usually testing log lines.

Run tests:
```bash
uv run pytest                # unit tests
uv run pytest -m integration # integration (requires real receivers)
uv run mypy src              # type check
uv run ruff check src tests  # lint
uv run ruff format --check . # format check
```

---

## Workflow expectations

### When starting a task

1. **Read `DESIGN.md`.** Confirm the task is in scope and you understand which phase it belongs to.
2. **Check existing code first.** Don't re-implement something that already exists in another module.
3. **State your plan briefly before coding** if the task is non-trivial. One paragraph is enough — what files change, what the approach is, what edge cases you'll handle.

### Commits

- **Conventional commits format.** `feat:`, `fix:`, `refactor:`, `test:`, `docs:`, `chore:`.
- **Scope is the module.** `feat(receivers): add HttpJsonReceiver retry logic`.
- **Body explains *why*, not *what*.** The diff shows the what. The body should answer "why this approach over alternatives."
- **One logical change per commit.** If you find yourself writing "and also" in a commit message, split it.
- **No commits with failing tests, lint errors, or type errors.** Run the checks before committing.

### When something is ambiguous

- **Ask, don't guess.** Especially for the open design questions in `DESIGN.md` (state persistence, position averaging, rate limiting, config reload, web UI).
- **Don't expand scope silently.** If you notice an issue outside the current task, log it as a TODO comment with a brief note, don't fix it inline.
- **Document decisions in code.** When picking between two reasonable approaches, leave a comment with the rationale. Future-you and Daniel will thank you.

### What to push back on

- Requests that conflict with `DESIGN.md`'s non-goals.
- Premature optimization — the polling loop runs at 1Hz; readability beats microbenchmarks.
- Adding dependencies for trivial functionality. Standard library first.
- Mocking what should be real — if a test mocks `httpx`, suggest `FileReceiver` instead.

---

## Phase awareness

The doc breaks work into phases. Stay within the current phase unless explicitly told otherwise:

- **Phase 1** — single receiver, no DB, no merger. Goal: a working pip-installable service that produces a "nearest aircraft" entity in HA.
- **Phase 2** — add reference databases and flag/alert rules.
- **Phase 3** — multi-receiver merger.
- **Phase 4** — Docker + HA add-on packaging.
- **Phase 5** — polish (AGL, orbit detection, predictive alerts, etc.).

If you're working on Phase 1 and notice the merger will need a particular hook, leave a `# TODO(phase-3): hook for merger` comment. Don't pre-build merger plumbing.

---

## Things that will trip you up

- **`aircraft.json` schema varies by receiver variant.** dump1090-fa, readsb, and dump1090-mutability have subtle differences in field names and types. The `HttpJsonReceiver` mapping logic needs to handle all of them. Test fixtures should include samples from each.
- **Hex codes can have a leading `~` for non-ICAO addresses (TIS-B, ADS-R).** Strip it on input but preserve the distinction in a flag — these are real aircraft, just not broadcasting their own ICAO.
- **`seen` and `seen_pos` are seconds-since-message, not absolute timestamps.** Convert at ingest using the `now` field from the JSON document.
- **`flight` (callsign) often has trailing whitespace.** dump1090 pads to 8 chars. Always `.strip()`.
- **MQTT retained messages need explicit cleanup.** Publishing `None`/empty payload with `retain=True` clears the retained value. Forgetting this leaves zombie aircraft in HA forever.
- **HA MQTT discovery topics are case-sensitive and have specific formatting.** Refer to the official docs every time; don't trust memory.
- **dump978 hex codes overlap with dump1090.** Same aircraft, same hex, different band. The merger handles this; receivers should not try to namespace hex codes.

---

## Useful references

- `DESIGN.md` — the spec
- dump1090 JSON schema: https://github.com/flightaware/dump1090/blob/master/README-json.md
- readsb JSON schema: https://github.com/wiedehopf/readsb/blob/dev/README-json.md
- HA MQTT discovery: https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery
- HA add-on config schema: https://developers.home-assistant.io/docs/add-ons/configuration/
- Mictronics DB format: https://github.com/wiedehopf/tar1090-db

---

## A note on tone

Daniel knows what he's doing — security background, deep homelab, hardware tinkerer. Skip the over-explanation and the disclaimers. When code or design choices are non-obvious, explain the reasoning concisely; don't pad with caveats. If you disagree with an approach, say so directly with the technical reason.
