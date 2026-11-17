# A cloud-off option cuts the TTLock cloud out on purpose, webhook included

Bluetooth (ADR 0003) and WiFi-local (ADR 0004) can read and drive a lock. Some users want the lock to work without the TTLock cloud, for privacy, through cloud outages, or to stop depending on the vendor. A lock in WiFi takeover is offline to the cloud anyway (-3036), so cloud control and cloud state reads would only fail.

We add a `cloud_enabled` option. It is on by default, so nothing changes until a user turns it off. With it off, HA makes no TTLock REST calls:

- Setup builds the entry from the cached key/list records, keys and detail. Only an entry with no cached lock list calls key/list, once.
- The poll reads state over BLE and WiFi-local only. It skips the slow tier, the door-sensor checks and the gateway coordinator.
- The cloud is not a lock/unlock transport. Local services with a cloud equivalent raise instead of falling back. Cloud-only services raise `cloud_disabled`. Credential names come only from the cache and from local adds.
- The WiFi proxy takes over a connecting lock instead of passing it through.
- The entry does not wire its per-lock webhook listeners, and `WebhookHandler.async_disable` unregisters the shared webhook. HA keeps the Nabu Casa cloudhook, so its URL survives turning the cloud back on. The unregister is best-effort and never fails entry setup.

The options flow refuses cloud off unless Bluetooth control or the WiFi listener is on. It also refuses cloud off while every cached lock lacks admin rights, because the local transports need the admin credentials.

_This qualifies AGENTS.md's "never trade the webhook push path for faster polling" and ADR 0002's "unregistered on the last member's unload"._ That rule stops push being swapped for polling as the way updates arrive. Cloud off changes no poll interval. It removes the whole cloud, in both directions, at the user's request. The webhook goes because it is part of the cloud. If it stayed registered, the service the user chose to cut off could still send in. With the cloud on, the push path is exactly as before. For ADR 0002, an entry with the cloud off now also unregisters the shared webhook at setup. It does this only when no other entry in its webhook group still has the cloud on. Otherwise the webhook stays for those entries, and this entry does not listen.

## Consequences

- **Updates come from HA's own reads.** With no push, a lock updates after HA's own commands, at each poll, and on the 5-minute local refresh. While the cloud is off, that refresh covers every lock that BLE or WiFi-local can reach. A lock event on a takeover session also requests a refresh.
- **The TTLock app loses remote features** for a taken-over lock. Local features on the lock itself still work.
- **Cached data goes stale.** HA does not refresh keys, detail, gateways or the lock list while the cloud is off. The exception is the saved settings, which HA reads from the lock over a local admin route. A change made in the TTLock app needs the cloud turned on again.
- **Turning the cloud back on** reloads the entry, and setup registers the webhook again at the same URL.
