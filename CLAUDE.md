# hass-ttlock

A Home Assistant custom integration (HACS) for TTLock smart locks. Domain: `ttlock`. Code lives entirely under `custom_components/ttlock/` as flat files — no subpackages. Keep it that way unless a restructure is explicitly requested; don't introduce a `helpers/`, `utils/`, or similar catch-all package.

## Feedback loop — run this before you're done

```
script/check
```

This is the one command that tells you whether a change is good: lint, type-check, then the full test suite, in that order, matching what CI enforces. Don't call a change finished without running it. Nothing here needs network access or real TTLock credentials — the test suite mocks the entire `TTLockApi` surface (see `mock_api_responses` in `tests/conftest.py`), so there's no excuse to skip it.

Each step is also runnable on its own when iterating on one thing:

- `script/lint` — format and auto-fix with ruff
- `script/lint-check` — ruff format/check without writing (what CI runs)
- `script/type-check` — `ty check`
- `script/test` — `pytest`, extra args pass through, e.g. `script/test -k coordinator` or `script/test -x` to stop on first failure

Fast inner loop while editing one file: `uv run ruff check --fix <file>` / `uv run ty check <file>` / `script/test -k <name>` are all quicker than the full `script/check` and fine to use mid-task — just run the full `script/check` before you consider the change done.

**Pytest is strict on purpose.** `filterwarnings = ["error"]` and `--strict-markers` are set in `pyproject.toml` — an unaddressed deprecation warning fails the suite, it doesn't just print. If a warning legitimately originates from a dependency (not this integration's own code), add a narrowly-scoped `ignore::` entry to `[tool.pytest.ini_options].filterwarnings` rather than silencing it elsewhere. This caught a real bug once already (a leaked `aiohttp.ClientSession` in a test fixture) — treat a new warning as a signal, not noise to suppress.

**Type checking uses [`ty`](https://github.com/astral-sh/ty)** (Astral's type checker), not mypy or pyright. Where `ty` flags a genuine tool/stub-precision limitation rather than a real bug (e.g. voluptuous's `vol.All` as a schema, HA's abstract `OAuth2Implementation` typing, dict/list generic invariance against HA's recursive `JsonValueType` alias), the fix is a narrow `# ty: ignore[rule] - reason` comment on that exact line, not a broader suppression. Before reaching for `ty: ignore`, check whether the diagnostic is actually a real bug — several genuine bugs (a stale import path, a `None`-handling gap that could crash diagnostics, a service response returning raw `datetime` objects instead of ISO strings) were found this way during the mypy→ty migration, not suppressed away.

## How the TTLock API works

This integration talks to TTLock's cloud API, not the locks directly — locks connect via a TTLock gateway or WiFi, and the gateway/lock relays commands from TTLock's cloud. Everything goes through `TTLockApi` in `api.py` — see its module docstring for auth and per-request quirk detail. Full API docs: `docs/ttlock-cloud-api/` (mirrored from https://euopen.ttlock.com/document; EU region — this integration is hardcoded to `https://euapi.ttlock.com`, there's no multi-region support).

State reaches HA two ways — polling (`coordinator.py`, 15-minute interval) and webhook push (`webhook.py`, near-real-time) — both merging into `LockUpdateCoordinator`. See each module's docstring for mechanism detail. Don't remove the webhook path in favor of "just poll faster."

## File map

- `api.py` — `TTLockApi`, the TTLock cloud REST client, plus the OAuth2 implementation classes
- `application_credentials.py` — wires up HA's `application_credentials` component for OAuth2 setup
- `config_flow.py` — OAuth2-based config flow (`TTLockAuthFlowHandler`)
- `const.py` — domain, service names, config keys
- `coordinator.py` — `LockUpdateCoordinator` (per-lock polling `DataUpdateCoordinator`, also merges in push updates from the webhook) and `GatewaysUpdateCoordinator` (polls TTLock gateways for online/offline status)
- `entity.py` — `BaseLockEntity`, the shared entity base most platform classes inherit from
- `models.py` — pydantic v2 models for TTLock API payloads (`Lock`, `LockState`, `Passcode`, `LockRecord`, etc.). Migrated from pydantic v1 — don't reintroduce v1 patterns (`Config` class, `.dict()`, unparameterized `Optional`)
- `webhook.py` — receives TTLock's push notifications; see its module docstring
- `services.py` / `services.yaml` — custom service actions (passcodes, passage mode, autolock, records, etc.)
- `diagnostics.py` — HA diagnostics export
- `lock.py`, `binary_sensor.py`, `sensor.py`, `switch.py` — entity platforms. `binary_sensor.py` includes both per-lock sensors (door sensor, passage mode) and `GatewaySensor` (per-gateway online/offline, from `GatewaysUpdateCoordinator`)
- `brand/` — local copy of the icon assets also submitted to `home-assistant/brands`
- `translations/` — `en.json` is the source of truth; mirror its structure when adding new strings, don't auto-translate the other languages

## Conventions

- Model fields that mirror the TTLock API's camelCase wire format on purpose (see `models.py`) are exempt from ruff's `N815` — don't rename them to snake_case.
- Tests live in `tests/`, mirroring `pytest_homeassistant_custom_component` conventions (`hass` fixture, `MockConfigEntry`, etc.). `conftest.py` has the shared fixtures — `mock_api_responses` mocks the whole `TTLockApi` surface via a scenario name, look there before adding a new one-off mock.
- Never commit or push without being explicitly asked — **except** when running the `/implement` skill, see `docs/agents/implement.md`.

## Agent skills

### Issue tracker

Issues are tracked as GitHub issues on `jbergler/hass-ttlock`, via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Default canonical labels (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context — `CONTEXT.md` + `docs/adr/` at the repo root, created lazily as needed. See `docs/agents/domain.md`.
