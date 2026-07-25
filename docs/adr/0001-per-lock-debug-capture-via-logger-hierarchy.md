# Per-lock debug capture is always-on, decoupled from the logger hierarchy

Recurring bug reports (#67, #266/#268) stalled for weeks because diagnostics only ever showed derived state, never the raw TTLock wire response — forcing a manual round trip of "enable debug logging, reproduce, paste logs" per report. The original design (superseded below) rode HA's logger hierarchy: give each lock a child logger (`custom_components.ttlock.device.<lockId>`) and gate a ring-buffer handler on `Logger.isEnabledFor`, so nothing was captured until a reporter's logger was raised to DEBUG (manually via Configure Logger, or a "Start debug capture" button, #290).

In practice that opt-in step was itself the friction the whole feature existed to remove: a reporter still had to be walked through *something* before a useful diagnostics download existed. We've since decided debug capture should be **always-on**: `LockTrafficCapture` (`capture.py`) is an in-memory, bounded, per-lock ring buffer populated unconditionally by `api.py` and `webhook.py` at the same call sites that already log — independent of any logger's level. A lock's diagnostics always contain its most recent traffic (redacted), no button, no Configure Logger step, no dead-end "capture was never enabled" state to explain to a reporter.

`custom_components.ttlock.device.<lockId>` (`get_device_logger`, const.py) still exists and is unaffected — it's now purely for a technical user who wants to *watch* a lock's traffic live via HA's stock Configure Logger UI (#287). It has no bearing on whether that traffic lands in the capture buffer; `api.py`/`webhook.py` call both the logger (opt-in, live view) and `LockTrafficCapture.capture` (always-on, diagnostics-facing) at each site, deliberately decoupled.

## Considered options

- **Always-on capture, no opt-in — now the chosen approach.** Originally rejected here over consent and redaction-completeness concerns; revisited because the opt-in step was undermining the feature's own goal (a diagnostics download sufficient without a round trip to the reporter), and capture already goes through the same field-level redaction as the rest of diagnostics before being surfaced. The residual risk (an unredacted field we haven't enumerated in `TO_REDACT`) is judged acceptable and no different in kind from what full debug logging already exposed to anyone who enabled it.
- **Gate capture on the per-lock logger's level (original design)** — superseded: worked, but reintroduced a manual step ("press the button" or "enable debug logging") that the feature was meant to eliminate, and left an empty-capture dead end for locks nobody had thought to instrument yet.
- **Bespoke per-lock switch/button with its own storage, independent of logging** — rejected (unchanged from original): duplicates HA's existing "enable debug logging" UX instead of reusing it for the *live* log view (#287), which we're keeping.
- **Gate on the integration's single existing debug-logging toggle** — rejected (unchanged from original): all-or-nothing across every lock, no way to isolate one problem device.

## Consequences

- `custom_components.ttlock.device.<lockId>` logger names are still intentional plumbing for the live/Configure-Logger-UI path (#287) — don't "clean them up" into a flat logger. They're just no longer load-bearing for whether diagnostics has content.
- #290 ("Start debug capture" button) is closed as obsolete: there's no "start" action left to trigger.
- Diagnostics content is bounded by buffer size and recency, not by an opt-in flag: a lock that's had no traffic since HA started (or since its buffer last wrapped) shows an empty `records` list — that's "nothing recent to show," not "capture was never enabled." There's no separate `active` field to explain the difference.
