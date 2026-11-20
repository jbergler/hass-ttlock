# Architecture

How the integration is wired together. Read this before editing anything under `custom_components/ttlock/`; the per-module detail lives in each file's docstring.

## Transports

The integration talks to TTLock three ways:

- **Cloud REST**: every call goes through `TTLockApi` in `api.py` (see its module docstring for auth and per-request quirks). Locks reach the cloud via a paired TTLock gateway or the lock's own WiFi radio. Full API reference: `docs/ttlock-cloud-api/` (mirrored from https://euopen.ttlock.com/document). The user picks the region (EU / International or China) in the config flow. `const.REGIONS` holds the two hosts, which differ only in hostname.
- **Local Bluetooth**: `ble.py` + `ble_protocol.py` talk to a lock directly via Home Assistant's `homeassistant.components.bluetooth`. Reads need only `aesKeyStr` from `lock/detail`. Lock/unlock also needs `lockKey` from the same response, and WiFi-local also needs `adminPwd`. `ble_protocol.decode_password` decodes them, so no mobile-app login is needed. Control is opt-in (`ble_control`). See `docs/adr/0003-local-ble-transport.md`.
- **WiFi-local**: `wifi_proxy.py` runs one TCP listener per HA instance on tcp/4999. It runs while at least one loaded entry has `wifi_local_enabled` on (default off) and a WiFi lock to register. A WiFi lock holds one outbound connection to `wifilock.ttlock.com:4999`. When the user's router redirects that connection to HA, the listener identifies the lock by the MAC in its login reply (the MAC filter). With the cloud on, it passes the lock through to the real cloud. With the cloud off, it takes the lock over and drives it locally. Pass-through falls back to takeover when the cloud host cannot be reached or resolves to HA itself. The frames use the same AES format as BLE (`ble_protocol.py`), with the `0x56` WiFi dialect marker. Only a takeover session can read or command the lock. A pass-through session only proves that the lock is connected. When HA holds the lock's key, a connection must prove the key before HA admits it. Every failure counts against the peer address. ADR 0004 gives the admission rules, the frame checks after admission, the backoff numbers, the connection caps, the replay residual and how two entries share a lock. See `docs/adr/0004-wifi-local-proxy.md`.

Every local credential comes from `lock/detail` (`aesKeyStr`, `lockKey`, `adminPwd`, `lockFlagPos`, `lockVersion`) plus the account `uid` from the OAuth token. `store.py` persists them on each `lock/detail` fetch, together with the non-secret detail and the passage-mode config. It also persists the entry's key/list records and `uid` on each successful enumeration, and the gateway list on each successful gateway refresh. Setup takes the lock list from key/list. When the cloud is off or key/list fails, it takes the list from the cache. A key/list failure also raises the `cloud_unreachable` repair. Setup then restores each lock's cached credentials and detail before any per-lock cloud fetch. So both local transports keep working through a cloud outage, and across a restart during one. Only an entry that has never enumerated needs the cloud to start. ADR 0003 records why the credentials are cached.

The lock/unlock handshake depends on the transport. Don't make one path match the other:

| | Bluetooth | WiFi-local |
|---|---|---|
| Lock / unlock | `CHECK_USER_TIME` -> token -> `UNLOCK`/`LOCK` | `CHECK_ADMIN` -> token -> `UNLOCK` (0x60) / `LOCK` (0x58) |

### Transport order and the cloud toggle

- **Lock / unlock** (`coordinator._async_drive`) tries each *available* transport in the user-set `transport_order`, and stops at the first success. `transport_order` is one of the six orderings, default BLE -> WiFi-local -> cloud. BLE is available when `ble_control` is on and the radio can reach the lock. WiFi-local is available when the lock has a ready takeover session. It is also available for a recent takeover client that is reconnecting, and the command waits up to `wifi_proxy.RECONNECT_WAIT` for it. The cloud is available when `cloud_enabled` is on. The coordinator logs a transport that declines or raises, and tries the next one. If nothing is available, it raises the translated `no_transport_to_lock` / `no_transport_to_unlock`. If every attempted transport fails, it raises `lock_failed` / `unlock_failed`, which names them.
- **Reads**: the fast-tier state check reads BLE and WiFi-local takeover (`wifi_proxy.async_status`, 0x14) in `transport_order`, then the cloud (`lock/queryOpenState`, only with the cloud on).
- **`cloud_enabled`** (default on). Off means zero cloud REST calls, except one key/list for an entry with no cached lock list. Setup uses the cached lock list. The poll skips the slow tier, the door-sensor checks and the cloud state read. There is no gateway coordinator and no cloud lock/unlock. ADR 0005 covers the rest: services, switches, the proxy mode, the webhook and the options-flow checks. See `docs/adr/0005-cloud-off-mode.md`.

## State propagation

State reaches HA four ways, merged by `LockUpdateCoordinator`:

1. **Polling**: `coordinator.py`, two tiers. The fast tier re-verifies lock state every `poll_interval` (default 30 minutes). The slow tier refreshes the ekey record, `lock/detail`, passage-mode config and the gateway list every `slow_poll_interval` (default 6 hours). The slow tier runs only with the cloud on. A failed slow-tier fetch keeps the previous values and never fails the poll. While the bolt is unread or stale, a takeover session that becomes ready requests a poll. Both intervals are options, with defaults in `const.py`.
2. **Webhook push**: `webhook.py`, near-real-time. See its module docstring. Not registered while the cloud is off.
3. **Local Bluetooth read**: serves the poll's state check when the lock is in range.
4. **WiFi-local read**: in takeover, serves the poll's state check (bolt and battery). BLE and WiFi-local are read in `transport_order`, the cloud last. An unsolicited lock event on the session requests a refresh.

A 5-minute local refresh (`LOCAL_REFRESH_INTERVAL`) reads the bolt over BLE and WiFi-local, in `transport_order`. It reads a lock only when the cloud is off or cannot reach it, and BLE or WiFi-local can.

**Don't** collapse the webhook path in favour of "just poll faster." **Don't** speed up polling to refresh the one value BLE already gives for free: a poll fetches more than lock state. Cloud-off mode drops the webhook because it removes the whole cloud, not to swap push for polling. See ADR 0005.

## File map

- `api.py`: `TTLockApi`, the TTLock cloud REST client, plus the OAuth2 implementation classes. Exposes the account `uid` the local handshakes embed.
- `application_credentials.py`: wires up HA's `application_credentials` component for OAuth2 setup.
- `ble.py`: Bluetooth glue over `homeassistant.components.bluetooth`: presence tracking per lock MAC, the GATT session that sends one command and reassembles the reply, and lock/unlock (`async_control`). All bluetooth imports are deferred and gated on the component being loaded.
- `ble_protocol.py`: the TTLock wire format as pure functions (framing, CRC, AES, `decode_password`, handshake builders), shared by Bluetooth and WiFi-local. No I/O and no logger, because it handles key material.
- `capture.py`: `LockTrafficCapture`, the always-on per-lock ring buffer behind diagnostics. See `docs/adr/0001-per-lock-debug-capture-via-logger-hierarchy.md`.
- `config_flow.py`: OAuth2-based config flow (`TTLockAuthFlowHandler`) and the options flow (polling, Bluetooth read and control, the WiFi-local listener, transport order, cloud on/off, and the checks that refuse unreachable combinations).
- `const.py`: domain, service names, config keys, option defaults, the proxy's port and upstream host.
- `coordinator.py`: `LockUpdateCoordinator` and `GatewaysUpdateCoordinator`. `LockUpdateCoordinator` is the per-lock polling `DataUpdateCoordinator`. It merges webhook pushes and local reads, owns what `connectable` means, and holds the local credentials. It runs the transport fan-out for lock/unlock, and computes the connection diagnostics. `GatewaysUpdateCoordinator` polls TTLock gateways for online/offline status, with the cloud on only.
- `entity.py`: `BaseLockEntity`, the shared entity base most platform classes inherit from. `LockDiagnosticEntity` is the always-available base for the connection diagnostics. `wifi_local_configured` gates the WiFi-local Link sensor.
- `models.py`: pydantic v2 models for TTLock API payloads (`Lock`, `LockState`, `Passcode`, `LockRecord`, etc.). Migrated from pydantic v1. Don't reintroduce v1 patterns (`Config` class, `.dict()`, unparameterized `Optional`).
- `store.py`: `LockStateStore`, persistence in `.storage`. Per lock it holds the cached local credentials, detail and passage config. Per entry it holds the key/list records, `uid` and gateways.
- `webhook.py`: receives TTLock's push notifications, shared per developer application (ADR 0002). `async_disable` unregisters it when the cloud is off and keeps the cloudhook (ADR 0005). See its module docstring.
- `wifi_proxy.py`: the in-HA WiFi listener and per-lock session registry: MAC filter, pass-through relay, local takeover (greet, config, keepalive), and the command and status path. It reuses `ble_protocol.py` and logs no secret values. `__init__.py` owns the lifecycle: one listener per HA instance, shared by the entries that opt in.
- `services.py` / `services.yaml`: custom service actions (passcodes, passage mode, autolock, records, etc.), all through the cloud. With the cloud off they raise `cloud_disabled`.
- `diagnostics.py`: HA diagnostics export. Redacts `TO_REDACT` keys.
- `lock.py`, `binary_sensor.py`, `sensor.py`, `switch.py`: entity platforms. `binary_sensor.py` includes per-lock sensors (door sensor, passage mode, the Cloud Online and Bluetooth Reachable connectivity diagnostics) and `GatewaySensor` (per-gateway online/offline, from `GatewaysUpdateCoordinator`). `sensor.py` adds the Connection and WiFi-local Link (with a `last_seen` attribute) diagnostics. `switch.py` has the auto-lock and lock-sound switches. They are admin-gated and set through the cloud, and exist only while the cloud is on.
- `brand/`: local copy of the icon assets also submitted to `home-assistant/brands`.
- `translations/`: `en.json` is the source of truth. Mirror its structure when adding new strings. Don't auto-translate the other languages.
