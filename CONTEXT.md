# hass-ttlock

A Home Assistant integration bridging TTLock locks into HA entities and services, over TTLock's cloud API, Bluetooth and WiFi-local.

## Language

**Connectable**:
Whether anything can currently reach a lock. Three things can: TTLock's cloud (with the cloud on, via a paired gateway or the lock's own WiFi radio), Home Assistant's own Bluetooth radio, or a WiFi-local **Takeover** session. Bluetooth counts when the lock is advertising in range, or a recent local read succeeded. A takeover session counts when it is ready, or when a recent takeover client is reconnecting. The cloud half is decided once when the lock is discovered. The Bluetooth and WiFi-local halves change while running, and polling starts and stops to match.
_Avoid_: reachable, online (online/offline is used for gateways specifically, a separate concept)

**Discovery**:
The one-time enumeration of every lock in a TTLock account, performed at integration startup. It reads `v3/key/list`, or the cached records when the cloud is off or key/list fails. Determines each lock's initial Connectable verdict.

**Ekey**:
TTLock's wire-level record for one account's access to one lock. Every account-to-lock relationship (owner of their own lock, recipient of a shared key) is represented as a single ekey. Discovery enumerates ekeys via `v3/key/list`, which is why Shared locks appear alongside owned ones.
_Avoid_: key (ambiguous with physical keys / API keys), credential (see **Credential**)

**Shared lock**:
A lock this account can see because someone else's administrator sent it an ekey. Discovery returns it the same way as an owned lock, but its Access level is `authorized` or `member` - not `owner` - so some actions are restricted.
_Avoid_: guest lock, foreign lock

**Access level**:
How the authenticated account relates to a lock. Three values: `owner` (top administrator - also in `v3/lock/list`); `authorized` (holds a common ekey with `keyRight=1`, operationally equivalent to `owner` for every REST endpoint the integration uses); `member` (common ekey with `keyRight=0` - may read state and lock/unlock if the admin left remote unlock on, but cannot read `lock/detail` so has no BLE, and cannot run any admin-only service). Mapped in `models.AccessLevel`; drives entity creation, service gating, and the `shared_lock_*` translation keys.
_Avoid_: permission, role

**Debug capture**:
Always-on recording of a single lock's raw TTLock traffic (API request/response bodies, inbound webhook events) into a small bounded in-memory buffer, surfaced through diagnostics. Not opt-in and not tied to any logger's level — see `docs/adr/0001-per-lock-debug-capture-via-logger-hierarchy.md`.
_Avoid_: debug logging (HA's own general, per-integration mechanism — Debug capture is per-lock, always-on, and diagnostics-facing), trace

**Developer application**:
The TTLock-registered app (identified by a `client_id`/`client_secret` pair, set up via HA's `application_credentials`) that a webhook callback URL is registered against in TTLock's Management Center. TTLock accepts exactly one callback URL per developer application — not per account. Multiple config entries (each a distinct TTLock account login) commonly share one developer application.
_Avoid_: account, app (ambiguous with the HA integration/entry itself)

**Webhook group**:
The set of config entries that share one developer application's `client_id`, and therefore share exactly one registered webhook. Registration is refcounted across the group — registered on the first member's setup, unregistered on the last member's unload — with no member acting as a privileged "owner." See `docs/adr/0002-shared-webhook-per-developer-application.md`.
_Avoid_: webhook owner

**Credential**:
A passcode, IC card or fingerprint that opens a lock (`CredentialNames`, `clear_credentials`, the Credentials sensor). In code, "credentials" also names a lock's keys from `lock/detail` (`WifiCredentials`). User-facing text calls those keys.
_Avoid_: using it for an Ekey

**Transport**:
One way of reaching a lock: `ble` (Bluetooth), `wifi_local` (**WiFi-local**) or `cloud` (TTLock REST). Lock and unlock try the available transports in the user's `transport_order` and stop at the first success. Admin ops and state reads try `ble` and `wifi_local` in `transport_order` too. Admin ops leave the cloud to the caller as a fallback. State reads try the cloud last.
_Avoid_: channel, path

**WiFi-local**:
The transport that drives a WiFi lock over its own cloud connection, once the user's network redirects that connection to Home Assistant's listener (`wifi_proxy.py`, tcp/4999). Opt-in per entry (`wifi_local_enabled`). Each connected lock is in **Pass-through** or **Takeover**. When HA holds the lock's key, a connection must prove the key before HA admits it (ADR 0004). The UI calls the listener the WiFi proxy.
_Avoid_: LAN mode, local Wi-Fi API

**Pass-through**:
A WiFi-local session while the cloud is on. HA relays the lock to the real TTLock cloud and only observes. The lock's reply to the cloud's first command is the key proof. A pass-through session proves that the redirect works. It cannot read or command the lock, so lock and unlock still go over the cloud. It falls back to **Takeover** when the cloud host cannot be reached or resolves to HA itself.
_Avoid_: relay mode, bridge mode

**Takeover**:
A WiFi-local session in which HA plays the cloud and drives the lock itself. This happens while the cloud is off, or after a pass-through fallback. The key proof is the lock's `f7 01` reply to HA's `F7`. The lock is offline to the TTLock cloud while taken over. Only a takeover session counts towards Connectable.
_Avoid_: local mode

**Cloud off**:
An entry with `cloud_enabled` off. HA makes no TTLock REST calls, except one key/list for an entry with no cached lock list. It unregisters the webhook but keeps any cloudhook, and takes WiFi-local locks over. See `docs/adr/0005-cloud-off-mode.md`.
_Avoid_: offline mode, no-cloud mode

**Outcome unknown**:
HA sent a local admin op that is not safe to repeat, and lost its answer (`LocalOutcomeUnknown`, translation key `local_outcome_unknown`). The lock may or may not have applied it, so HA never retries it over another transport or the cloud.
_Avoid_: timeout, failed
