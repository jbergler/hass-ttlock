# Architecture

How the integration is wired together. Read this before editing anything under `custom_components/ttlock/`; the per-module detail lives in each file's docstring.

## Transports

The integration talks to TTLock two ways:

- **Cloud REST** — every call goes through `TTLockApi` in `api.py` (see its module docstring for auth and per-request quirks). Locks reach the cloud via a paired TTLock gateway or the lock's own WiFi radio. Full API reference: `docs/ttlock-cloud-api/` (mirrored from https://euopen.ttlock.com/document).
- **Local Bluetooth** — `ble.py` + `ble_protocol.py` talk to a lock directly via Home Assistant's `homeassistant.components.bluetooth`, for state reads only. See `docs/adr/0003-local-ble-transport.md` for what the radio can and can't do.

**EU region only.** The cloud URL is hardcoded to `https://euapi.ttlock.com` — there is no multi-region support.

## State propagation

State reaches HA three ways, merged by `LockUpdateCoordinator`:

1. **Polling** — `coordinator.py`, 15-minute interval. Each poll also fetches `lock/detail`, passage mode, and the gateway list, not just lock state.
2. **Webhook push** — `webhook.py`, near-real-time. See its module docstring.
3. **Local Bluetooth read** — serves the poll's state check when the lock is in range, and runs a 5-minute local refresh for locks the cloud cannot reach.

**Don't** collapse the webhook path in favour of "just poll faster." **Don't** speed up polling to refresh the one value BLE already gives for free — a poll fetches more than lock state.

## File map

- `api.py` — `TTLockApi`, the TTLock cloud REST client, plus the OAuth2 implementation classes.
- `application_credentials.py` — wires up HA's `application_credentials` component for OAuth2 setup.
- `ble.py` — Bluetooth glue over `homeassistant.components.bluetooth`: presence tracking per lock MAC, and the GATT session that sends one command and reassembles the reply. All bluetooth imports are deferred and gated on the component being loaded.
- `ble_protocol.py` — the TTLock BLE wire format as pure functions (framing, CRC, AES); no I/O, no logger — it handles key material.
- `config_flow.py` — OAuth2-based config flow (`TTLockAuthFlowHandler`).
- `const.py` — domain, service names, config keys.
- `coordinator.py` — `LockUpdateCoordinator` (per-lock polling `DataUpdateCoordinator`, also merges in push updates from the webhook and local reads over Bluetooth; owns what `connectable` means) and `GatewaysUpdateCoordinator` (polls TTLock gateways for online/offline status).
- `entity.py` — `BaseLockEntity`, the shared entity base most platform classes inherit from.
- `models.py` — pydantic v2 models for TTLock API payloads (`Lock`, `LockState`, `Passcode`, `LockRecord`, etc.). Migrated from pydantic v1 — don't reintroduce v1 patterns (`Config` class, `.dict()`, unparameterized `Optional`).
- `webhook.py` — receives TTLock's push notifications; see its module docstring.
- `services.py` / `services.yaml` — custom service actions (passcodes, passage mode, autolock, records, etc.).
- `diagnostics.py` — HA diagnostics export.
- `lock.py`, `binary_sensor.py`, `sensor.py`, `switch.py` — entity platforms. `binary_sensor.py` includes both per-lock sensors (door sensor, passage mode) and `GatewaySensor` (per-gateway online/offline, from `GatewaysUpdateCoordinator`).
- `brand/` — local copy of the icon assets also submitted to `home-assistant/brands`.
- `translations/` — `en.json` is the source of truth; mirror its structure when adding new strings, don't auto-translate the other languages.
