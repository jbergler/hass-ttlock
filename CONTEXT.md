# hass-ttlock

A Home Assistant integration bridging TTLock's cloud API into HA entities and services.

## Language

**Connectable**:
Whether TTLock's cloud can currently reach a lock — via a paired gateway or the lock's own WiFi radio. Decided once when the lock is discovered; a non-connectable lock is never polled again on the assumption connectivity won't change until the integration reloads.
_Avoid_: reachable, online (online/offline is used for gateways specifically, a separate concept)

**Discovery**:
The one-time enumeration of every lock in a TTLock account, performed at integration startup. Determines each lock's initial Connectable verdict.

**Debug capture**:
Always-on recording of a single lock's raw TTLock traffic (API request/response bodies, inbound webhook events) into a small bounded in-memory buffer, surfaced through diagnostics. Not opt-in and not tied to any logger's level — see `docs/adr/0001-per-lock-debug-capture-via-logger-hierarchy.md`.
_Avoid_: debug logging (HA's own general, per-integration mechanism — Debug capture is per-lock, always-on, and diagnostics-facing), trace
