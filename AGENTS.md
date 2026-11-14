# hass-ttlock

A Home Assistant custom integration (HACS) for TTLock smart locks. Domain: `ttlock`.

## Must not

- **Never commit or push without being asked** — except when running the `/implement` skill (see `docs/agents/implement.md`).
- **Never restructure `custom_components/ttlock/` into subpackages** (no `helpers/`, `utils/`, etc.). It is flat on purpose.
- **Never reintroduce pydantic v1 patterns** in `models.py` (`Config` class, `.dict()`, unparameterized `Optional`).
- **Never trade the webhook push path for faster polling**, and never speed up polling just to refresh what BLE already gives for free. See `docs/architecture.md`.
- **Never suppress a `ty` diagnostic or a pytest warning without first checking it isn't a real bug.** See `docs/testing.md`.

## Feedback loop

`script/check` **must pass before every push that opens or updates a PR** — no exceptions, including docs-only or "obviously safe" changes. Lint, type-check, then the full test suite, in that order, matching CI. If it fails, fix it first; never push and rely on CI to tell you.

```
script/check
```

No network or real credentials required; `tests/conftest.py`'s `mock_api_responses` covers the whole `TTLockApi` surface.

Narrower steps for iteration:

- `script/lint` — ruff format and auto-fix
- `script/lint-check` — ruff format/check without writing (CI parity)
- `script/type-check` — `ty check`
- `script/test [-k name] [-x]` — pytest, extra args pass through

Even narrower while editing one file: `uv run ruff check --fix <file>`, `uv run ty check <file>`, `script/test -k <name>`. Still run `script/check` before done.

## Conventions

- Model fields that mirror the TTLock API's camelCase wire format are exempt from ruff's `N815` — don't rename them to snake_case.
- `en.json` under `translations/` is the source of truth; mirror its structure when adding strings, don't auto-translate the others.

## Where to look next

- **`docs/architecture.md`** — transports (cloud REST, local BLE), state propagation (polling / webhook / BLE merge), full file map. Read before editing anything under `custom_components/ttlock/`.
- **`docs/testing.md`** — strict pytest and `ty` detail; read before suppressing a warning.
- **`CONTEXT.md`** — domain glossary (Connectable, Discovery, Debug capture, Developer application, Webhook group). Use these exact terms.
- **`docs/adr/`** — architectural decisions. Read the ADR touching your area before changing its mechanism.
- **`docs/ttlock-cloud-api/`** — mirrored TTLock cloud API reference (EU region).
- **`docs/agents/`** — agent workflow skills: `implement.md` (PR workflow), `domain.md` (glossary + ADR etiquette), `issue-tracker.md` (`gh` CLI), `triage-labels.md`.
