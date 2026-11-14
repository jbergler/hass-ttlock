# hass-ttlock

A Home Assistant integration bridging TTLock's cloud API into HA entities and services.

## Language

**Connectable**:
Whether anything can currently reach a lock: TTLock's cloud (via a paired gateway or the lock's own WiFi radio) or Home Assistant's own Bluetooth radio (the lock is advertising in range, or a recent local read succeeded). The cloud half is decided once when the lock is discovered; the Bluetooth half changes while running, and polling starts and stops to match.
_Avoid_: reachable, online (online/offline is used for gateways specifically, a separate concept)

**Discovery**:
The one-time enumeration of every lock in a TTLock account, performed at integration startup. Determines each lock's initial Connectable verdict.

**Ekey**:
TTLock's wire-level record for one account's access to one lock. Every account-to-lock relationship (owner of their own lock, recipient of a shared key) is represented as a single ekey. Discovery enumerates ekeys via `v3/key/list`, which is why Shared locks appear alongside owned ones.
_Avoid_: key (ambiguous with physical keys / API keys), credential

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
