# hass-ttlock

A Home Assistant integration bridging TTLock's cloud API into HA entities and services.

## Language

**Connectable**:
Whether TTLock's cloud can currently reach a lock — via a paired gateway or the lock's own WiFi radio. Decided once when the lock is discovered; a non-connectable lock is never polled again on the assumption connectivity won't change until the integration reloads.
_Avoid_: reachable, online (online/offline is used for gateways specifically, a separate concept)

**Discovery**:
The one-time enumeration of every lock in a TTLock account, performed at integration startup. Determines each lock's initial Connectable verdict.

**Debug capture**:
Opt-in recording of a single lock's raw TTLock traffic (API request/response bodies, inbound webhook events) for troubleshooting, surfaced through diagnostics.
_Avoid_: debug logging (HA's own general, per-integration mechanism — Debug capture is per-lock and diagnostics-facing), trace
