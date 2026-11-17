# Talk to locks over Home Assistant's own Bluetooth, with the cloud as the fallback

Every lock/unlock, state read and event used to go `HA → TTLock cloud → gateway or WiFi → lock`, so a lock three metres from the HA host was unusable without a paired gateway (#268, #244) and every state read spent cloud quota (#249). TTLock locks are Bluetooth devices first; the gateway only relays. This adds a local transport over `homeassistant.components.bluetooth`. It reads a lock and, when the user opts in, drives it. It leaves the cloud path exactly as it was for everything the radio can't do.

The credentials come from the cloud API this integration already authenticates against. No second login against the TTLock mobile-app account is needed, which is what the BLE-only integrations do. `lock/detail` returns:

- `aesKeyStr`, the AES-128 key that encrypts every frame, and `lockVersion`, the protocol dialect. `QUERY_LOCK_STATUS` needs no authenticated session, so with these alone a lock in range reports bolt, battery and door state locally.
- `lockKey` (the `unlockKey`) and `adminPwd` (the model's `noKeyPwd`, the admin password). Each is obfuscated as base64 of a decimal-byte CSV, XORed with a fixed table. `ble_protocol.decode_password` recovers both. The lock accepts writes with these two, `lockFlagPos` from the same response, and the account `uid` from the OAuth token.

The writes:

- **lock/unlock**: `CHECK_USER_TIME` -> token -> `UNLOCK`/`LOCK` carrying `(token + unlockKey)`. This worked on a real lock through an ESPHome Bluetooth proxy at about -92 dBm, on an earlier revision. Later revisions are covered by tests only.
- **admin ops** (settings, passcodes, cards, fingerprints): `CHECK_ADMIN` -> token -> `CHECK_RANDOM` echoing `(token + unlockKey)` -> the op. The handshake is verified on hardware over WiFi-local (ADR 0004), which uses the same credentials. Over Bluetooth, only unit tests cover it. Some ops are not yet confirmed on hardware on either transport. `services.yaml` marks each one.

`ble_protocol.py` is the wire format - framing, CRC-8/MAXIM, AES-CBC - as pure functions with no I/O, written from the protocol as documented by the public reference implementations, not ported from them (the main one is GPL). `ble.py` owns the GATT session. The coordinator prefers the local read and falls back to `lock/queryOpenState`. There is no abstract transport class for reads or lock/unlock, because the transports have genuinely different capabilities and a shared interface would have to lie about one of them. Admin ops are the exception. `lock_commands.AdminSession` is a one-method protocol that sends an opcode and returns the decrypted reply. Bluetooth and WiFi-local implement it, because they share the wire format and the admin handshake. The cloud is not behind it. Its admin endpoints are REST calls keyed by cloud ids, not lock opcodes. So the cloud stays the caller's fallback where an equivalent exists.

## What the radio can and can't do

- **Reads work.** Verified on a KT170 with the gateway unplugged: the poll reports lock, battery and door over BLE.
- **Writes are opt-in.** Lock/unlock over BLE is behind its own option, **Control locks over Bluetooth**, which is off by default. BLE admin ops need it too. The list of writes above says how each one was tested.
- **The lock pushes nothing.** With an authenticated session held open through four manual operations, nothing arrived on the notify characteristic, and the advertisement's manufacturer data is byte-for-byte identical locked and unlocked. Polling is the whole update path for a lock without a gateway.

## Consequences

- **`connectable` is a question, not a verdict.** It means *anything can reach the lock*: the cloud half is decided once from `hasGateway`/WiFi; the radio half changes while running and polling starts and stops to match. A lock advertises in bursts minutes apart, so a recent successful read stands as proof of reachability between them, and a lock the radio can hear rides out a couple of failed reads, retried a minute apart, before going unavailable - and then goes back to its normal poll, so a lock that stays silent isn't dialled every minute for its battery's sake.
- **A separate local refresh timer, not a faster poll.** A poll is not one request - it also fetches `lock/detail`, passage mode and the gateway list - so polling faster to refresh the one value that is free would multiply the quota spend. The timer reads state only, over the local transports only, and only when the cloud is off or can't reach the lock. A lock with a gateway has the webhook, which is faster and costs the lock's battery nothing.
- **The radio can be turned off.** A boolean in the integration's options, on by default. Off means no presence tracking, no local reads, no local control, no signal sensor and no grace: the lock behaves exactly as it did before this transport existed. It's the exit for anyone whose adapter or lock misbehaves, without uninstalling anything.
- **Credentials are cached.** `lock/detail` is their only source, and it needs the cloud. So `store.py` persists them to `.storage` on each fetch, inside the same trust boundary as the OAuth token. It caches the entry's lock list too. A lock in range stays drivable through a cloud outage, and across a restart during one.
- **The webhook path stays.** For a lock with a gateway it remains the near-real-time path; nothing local replaces it.
- **A proxy needs no code.** HA's bluetooth component presents ESPHome proxies through the same API as a local adapter, so range can be extended later without touching this integration.
- **No key material is logged.** `ble_protocol.py` has no logger by design, and `ble.py` logs only addresses and connection errors: a debug line carrying a key, plaintext or decoded frame would leak into logs attached to bug reports.
