# Testing and type checking

## Strict pytest

`pyproject.toml` sets `filterwarnings = ["error"]` and `--strict-markers`. An unaddressed deprecation warning **fails** the suite, it does not just print. This has already caught a real bug once (a leaked `aiohttp.ClientSession` in a test fixture) — treat a new warning as a signal, not noise to suppress.

If a warning legitimately originates from a dependency (not this integration's own code), add a narrowly-scoped `ignore::` entry to `[tool.pytest.ini_options].filterwarnings`. Don't silence it elsewhere.

## Test fixtures

Tests live in `tests/`, mirroring `pytest_homeassistant_custom_component` conventions (`hass` fixture, `MockConfigEntry`, etc.). `tests/conftest.py` holds the shared fixtures.

The whole `TTLockApi` surface is mocked via `mock_api_responses` keyed by scenario name — look there before adding a new one-off mock. No test needs network or real TTLock credentials.

## Type checker: `ty`, not mypy / pyright

This repo uses [`ty`](https://github.com/astral-sh/ty) (Astral's type checker). Where `ty` flags a genuine tool/stub-precision limitation rather than a real bug — for example:

- voluptuous's `vol.All` used as a schema,
- HA's abstract `OAuth2Implementation` typing,
- dict/list generic invariance against HA's recursive `JsonValueType` alias,

— the fix is a narrow `# ty: ignore[rule] - reason` comment on that exact line, not a broader suppression.

**Before reaching for `ty: ignore`, check whether the diagnostic is actually a real bug.** Several genuine bugs — a stale import path, a `None`-handling gap that could crash diagnostics, a service response returning raw `datetime` objects instead of ISO strings — were found this way during the mypy→ty migration rather than suppressed away.
