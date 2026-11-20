# Architecture

How the integration is wired together. Read this before editing anything under `custom_components/ttlock/`; the per-module detail lives in each file's docstring.

## Transports

The integration talks to TTLock two ways:

- **Cloud REST**: every call goes through `TTLockApi` in `api.py` (see its module docstring for auth and per-request quirks). Locks reach the cloud via a paired TTLock gateway or the lock's own WiFi radio. Full API reference: `docs/ttlock-cloud-api/` (mirrored from https://euopen.ttlock.com/document). The user picks the region (EU / International or China) in the config flow. `const.REGIONS` holds the two hosts, which differ only in hostname.
- **Local Bluetooth**: `ble.py` + `ble_protocol.py` talk to a lock directly via Home Assistant's `homeassistant.components.bluetooth`. Reads need only `aesKeyStr` from `lock/detail`. Lock/unlock also needs `lockKey` from the same response. `ble_protocol.decode_password` decodes it, so no mobile-app login is needed. Control is opt-in (`ble_control`). See `docs/adr/0003-local-ble-transport.md`.

Every local credential comes from `lock/detail` (`aesKeyStr`, `lockKey`, `lockFlagPos`, `lockVersion`) plus the account `uid` from the OAuth token. `store.py` persists them on each `lock/detail` fetch, together with the non-secret detail and the passage-mode config. It also persists the entry's key/list records and `uid` on each successful enumeration, and the gateway list on each successful gateway refresh. Setup takes the lock list from key/list. When key/list fails, it takes the list from the cache and raises the `cloud_unreachable` repair. Setup then restores each lock's cached credentials and detail before any per-lock cloud fetch. So Bluetooth keeps working through a cloud outage, and across a restart during one. Only an entry that has never enumerated needs the cloud to start. ADR 0003 records why the credentials are cached.

Lock/unlock over Bluetooth is `CHECK_USER_TIME` -> token -> `UNLOCK`/`LOCK`.

### Transport order

- **Lock / unlock** (`coordinator._async_drive`) tries each *available* transport in the user-set `transport_order`, and stops at the first success. `transport_order` is BLE -> cloud by default, or cloud -> BLE. BLE is available when `ble_control` is on and the radio can reach the lock. The cloud always is. The coordinator logs a transport that declines or raises, and tries the next one. If every attempted transport fails, it raises the translated `lock_failed` / `unlock_failed`, which names them.
- **Reads** ignore `transport_order`: the fast-tier state check is always BLE -> cloud (`lock/queryOpenState`).
## State propagation

State reaches HA three ways, merged by `LockUpdateCoordinator`:

1. **Polling**: `coordinator.py`, two tiers. The fast tier re-verifies lock state every `poll_interval` (default 30 minutes). The slow tier refreshes the ekey record, `lock/detail`, passage-mode config and the gateway list every `slow_poll_interval` (default 6 hours). A failed slow-tier fetch keeps the previous values and never fails the poll. Both intervals are options, with defaults in `const.py`.
2. **Webhook push**: `webhook.py`, near-real-time. See its module docstring.
3. **Local Bluetooth read**: serves the poll's state check when the lock is in range.

A 5-minute local refresh (`LOCAL_REFRESH_INTERVAL`) reads the bolt over BLE. It reads a lock only when the cloud cannot reach it and BLE can.

**Don't** collapse the webhook path in favour of "just poll faster." **Don't** speed up polling to refresh the one value BLE already gives for free: a poll fetches more than lock state.

## File map

- `api.py`: `TTLockApi`, the TTLock cloud REST client, plus the OAuth2 implementation classes. Exposes the account `uid` the local handshakes embed.
- `application_credentials.py`: wires up HA's `application_credentials` component for OAuth2 setup.
- `ble.py`: Bluetooth glue over `homeassistant.components.bluetooth`: presence tracking per lock MAC, the GATT session that sends one command and reassembles the reply, and lock/unlock (`async_control`). All bluetooth imports are deferred and gated on the component being loaded.
- `ble_protocol.py`: the TTLock BLE wire format as pure functions (framing, CRC, AES, `decode_password`, handshake builders). No I/O and no logger, because it handles key material.
- `capture.py`: `LockTrafficCapture`, the always-on per-lock ring buffer behind diagnostics. See `docs/adr/0001-per-lock-debug-capture-via-logger-hierarchy.md`.
- `config_flow.py`: OAuth2-based config flow (`TTLockAuthFlowHandler`) and the options flow (polling, Bluetooth read and control, transport order, and the check that refuses Bluetooth control without Bluetooth reads).
- `const.py`: domain, service names, config keys, option defaults.
- `coordinator.py`: `LockUpdateCoordinator` and `GatewaysUpdateCoordinator`. `LockUpdateCoordinator` is the per-lock polling `DataUpdateCoordinator`. It merges webhook pushes and local reads, owns what `connectable` means, and holds the local credentials. It runs the transport fan-out for lock/unlock, and computes the connection diagnostics. `GatewaysUpdateCoordinator` polls TTLock gateways for online/offline status.
- `entity.py`: `BaseLockEntity`, the shared entity base most platform classes inherit from. `LockDiagnosticEntity` is the always-available base for the connection diagnostics.
- `models.py`: pydantic v2 models for TTLock API payloads (`Lock`, `LockState`, `Passcode`, `LockRecord`, etc.). Migrated from pydantic v1. Don't reintroduce v1 patterns (`Config` class, `.dict()`, unparameterized `Optional`).
- `store.py`: `LockStateStore`, persistence in `.storage`. Per lock it holds the cached local credentials, detail and passage config. Per entry it holds the key/list records, `uid` and gateways.
- `webhook.py`: receives TTLock's push notifications, shared per developer application (ADR 0002). See its module docstring.
- `services.py` / `services.yaml`: custom service actions (passcodes, passage mode, autolock, records, etc.).
- `diagnostics.py`: HA diagnostics export. Redacts `TO_REDACT` keys.
- `lock.py`, `binary_sensor.py`, `sensor.py`, `switch.py`: entity platforms. `binary_sensor.py` includes per-lock sensors (door sensor, passage mode, the Cloud Online and Bluetooth Reachable connectivity diagnostics) and `GatewaySensor` (per-gateway online/offline, from `GatewaysUpdateCoordinator`). `sensor.py` adds the Connection diagnostic.
- `brand/`: local copy of the icon assets also submitted to `home-assistant/brands`.
- `translations/`: `en.json` is the source of truth. Mirror its structure when adding new strings. Don't auto-translate the other languages.
