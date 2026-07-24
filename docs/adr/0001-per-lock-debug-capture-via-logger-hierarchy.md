# Per-lock debug capture rides on HA's logger hierarchy, not a bespoke buffer

Recurring bug reports (#67, #266/#268) stalled for weeks because diagnostics only ever showed derived state, never the raw TTLock wire response — forcing a manual round trip of "enable debug logging, reproduce, paste logs" per report. We considered a dedicated capture mechanism (a per-lock switch/button backed by its own buffer and its own opt-in flag) before deciding to instead give each lock a child logger (`custom_components.ttlock.device.<lockId>`) and attach one ring-buffer `logging.Handler` to the integration's base logger, filtering by `record.name`.

This means Debug capture isn't a separate system: it's just HA's existing per-logger level check (`Logger.isEnabledFor`) gating whether our handler ever sees a record. Power users can target one lock directly via HA's stock Configure Logger UI with no new code; the "Start debug capture" button per lock is pure sugar over the same mechanism — it calls the `logger.set_level` service for that lock's logger and schedules an automatic revert, rather than running a parallel capture path.

## Considered options

- **Always-on capture, no opt-in** — rejected: raw payloads can't be reliably scrubbed of every sensitive field, and users should consent to that data being buffered at all.
- **Bespoke per-lock switch/button with its own storage, independent of logging** — rejected: duplicates HA's existing "enable debug logging" UX instead of reusing it, and gives technical users no way to target a single lock without us building a second control surface.
- **Gate on the integration's single existing debug-logging toggle** — rejected: all-or-nothing across every lock: no way to isolate one problem device without flooding the buffer/log with every other lock's traffic too.

## Consequences

- `custom_components.ttlock.device.<lockId>` logger names are intentional plumbing, not incidental — don't "clean them up" into a flat logger.
- Diagnostics content is only as good as what's been captured: if a lock's logger was never at DEBUG, its capture section is empty. This is expected, not a bug — the workflow is still "ask the reporter to press the button (or enable debug logging) and reproduce," diagnostics just replaces "paste logs" with "download this."
