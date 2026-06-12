# Phase 4 implementation plan — Docker + HA add-on packaging

**Goal (DESIGN "Distribution"):** the functionally-complete service becomes
installable the three documented ways — **HA add-on (primary)**, **Docker
image**, **pip** (already works) — and the published MQTT payload contract is
frozen with `schema_version: 1`. This is the gate to depending on it long-term
and to a public release.

Nothing about the runtime changes. Phase 4 is wrapping, not features: a
container the service runs in, an add-on shim that translates HA options into
our YAML config, and a one-field contract lock.

---

## 0. Starting point

- `ha-airspace` is a pip-installable package (`[project.scripts] ha-airspace =
  ha_airspace.cli:main`); `python -m ha_airspace -c config.yaml` already runs
  the full multi-source service end to end.
- Config is a single YAML file (`-c PATH`, required). `/data`-friendly paths are
  already the documented defaults (`journal.path: /data/journal.db`,
  `databases.cache_dir: /data/db`).
- No `Dockerfile`, no `addon/`, no CI. The MQTT payloads carry **no**
  `schema_version` yet (deliberately deferred to Phase 4 — DESIGN §"schema
  versioning during pre-public phases").

## Naming / registry decisions (locked for this phase)

- Canonical name **`ha-airspace`** (matches the package + git remote
  `ifnull/ha-airspace`). The stale `ha-squitter` URLs in `pyproject.toml` get
  corrected to the real repo as part of Slice 1.
- Image **`ghcr.io/ifnull/ha-airspace`**. Docker Hub can come later.
- Arch targets **amd64 + arm64 + armv7** (Pi coverage). Base image
  **`python:3.12-slim`** (glibc) over Alpine — manylinux wheels for
  `pydantic-core`/`httpx` install cleanly; musl would force painful armv7 source
  builds.

---

## Build order (three slices; CI deferred)

### Slice 1 — Docker image + schema lock (the foundation)
Everything else builds on the base image, so it lands first.

- **`schema_version: 1`** on the published entity payloads. A module constant
  `PAYLOAD_SCHEMA_VERSION = 1` in `mqtt/payloads.py`, added as the first field of
  `AircraftPayload` and `DronePayload`. Those two classes are reused by the
  publisher for the wildcard, alert, nearest-aircraft, and nearest-drone topics,
  so the version propagates to the whole consumer-facing surface in one place.
  Receiver stats/location payloads stay unversioned (internal diagnostics).
- **Root `Dockerfile`** — multi-stage: a `uv`-based build stage producing a
  wheel + locked deps, copied into a slim `python:3.12-slim` runtime. Non-root
  user, `WORKDIR`, `VOLUME ["/config", "/data"]`,
  `ENTRYPOINT ["ha-airspace"]`, `CMD ["-c", "/config/config.yaml"]`. A
  lightweight `HEALTHCHECK` (`ha-airspace --version`, proving the entrypoint
  imports cleanly — no network dependency).
- **`.dockerignore`** — exclude `.venv`, `.git`, tests, `htmlcov`, caches, and
  **`config.local.yaml`** (never bake real creds into a layer).
- **`pyproject.toml`** URL fix (`ha-squitter` → `ha-airspace`).
- **Verification:** `docker build` succeeds; `docker run --rm IMAGE --version`
  prints the version; a bad/missing config exits 2 with a legible message. The
  `schema_version` field is covered by a unit test in the normal gate.

### Slice 2 — HA add-on wrapper (`addon/`, in-repo per CLAUDE.md layout)
- `addon/config.yaml` — add-on metadata + options schema mapping the HA options
  UI onto our config (receivers, watchpoints, MQTT, flag/alert toggles, log
  level). `/data` persistence for the journal + DB cache; declares the optional
  `/metrics` port.
- `addon/Dockerfile` — thin, `FROM ghcr.io/ifnull/ha-airspace:<tag>` (or the
  HA base image + `pip install`), adding the run shim.
- `addon/run.sh` — `bashio`: read add-on options → render `/data/config.yaml` →
  `exec ha-airspace -c /data/config.yaml`. Pull MQTT host/credentials from the
  HA Mosquitto service when the user leaves them blank.
- `addon/repository.yaml`, `addon/README.md` / `DOCS.md`, `addon/icon.png`
  placeholder.

### Slice 3 — CI multi-arch publish (DEFERRED, not this phase)
`.github/workflows`: lint/type/test gate on PR; multi-arch buildx → GHCR on tag;
add-on image build. Explicitly out of scope now per Daniel — Slices 1–2 first.

---

## Out of scope / non-goals

- No runtime/feature changes. If a packaging need exposes a real bug, log a TODO
  and keep it out of this phase.
- No web UI / ingress (DESIGN defers that to v1.1 / Phase 4.5).
- No Docker Hub mirror yet (GHCR only).

## Testing

- `schema_version` field present + equal to 1 on aircraft & drone payloads
  (unit test, normal gate).
- Dockerfile validated by an actual `docker build` + `docker run --version`
  smoke (manual/CI, not pytest — image builds are not unit tests).
- Add-on `run.sh` option→config translation: a small shell or python test that
  renders config from a sample options blob and asserts `load_config` accepts it
  (keeps the translation honest without a live Supervisor).
