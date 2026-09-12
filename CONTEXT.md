# hass-ttlock

A Home Assistant integration bridging TTLock's cloud API into HA entities and services.

## Language

**Connectable**:
Whether anything can currently reach a lock: TTLock's cloud (via a paired gateway or the lock's own WiFi radio) or Home Assistant's own Bluetooth radio (the lock is advertising in range, or a recent local read succeeded). The cloud half is decided once when the lock is discovered; the Bluetooth half changes while running, and polling starts and stops to match.
_Avoid_: reachable, online (online/offline is used for gateways specifically, a separate concept)

**Discovery**:
The one-time enumeration of every lock in a TTLock account, performed at integration startup. Determines each lock's initial Connectable verdict.

**Debug capture**:
Always-on recording of a single lock's raw TTLock traffic (API request/response bodies, inbound webhook events) into a small bounded in-memory buffer, surfaced through diagnostics. Not opt-in and not tied to any logger's level — see `docs/adr/0001-per-lock-debug-capture-via-logger-hierarchy.md`.
_Avoid_: debug logging (HA's own general, per-integration mechanism — Debug capture is per-lock, always-on, and diagnostics-facing), trace

**Developer application**:
The TTLock-registered app (identified by a `client_id`/`client_secret` pair, set up via HA's `application_credentials`) that a webhook callback URL is registered against in TTLock's Management Center. TTLock accepts exactly one callback URL per developer application — not per account. Multiple config entries (each a distinct TTLock account login) commonly share one developer application.
_Avoid_: account, app (ambiguous with the HA integration/entry itself)

**Webhook group**:
The set of config entries that share one developer application's `client_id`, and therefore share exactly one registered webhook. Registration is refcounted across the group — registered on the first member's setup, unregistered on the last member's unload — with no member acting as a privileged "owner." See `docs/adr/0002-shared-webhook-per-developer-application.md`.
_Avoid_: webhook owner
