---
name: Issue
about: Report a bug
title: ""
labels: ""
assignees: jbergler
---

<!---
Bug reports which do not follow this template will be closed.

Please keep every heading below (the lines starting with ## or ###) exactly
as they are — only replace the placeholder text underneath each one. This
issue is checked automatically against these headings; removing one (even
if you've answered the question) will get the issue auto-closed.
-->

## Describe the bug

A clear and concise description of what the bug is.

### To Reproduce

Steps to reproduce the behavior:

1. Go to '...'
2. Click on '....'
3. Scroll down to '....'
4. See error

### Expected behavior

A clear and concise description of what you expected to happen.

## Required information

<!---
Note: if this information is not provided, the issue will likely be closed without investigation as I am unable to make progress.
-->

Please provide:

- If this bug is about a specific lock: that lock's diagnostics. Go to the lock's device page (Settings → Devices & services → TTLock → click the lock) and use the three-dot menu → **Download diagnostics**. This automatically includes that lock's recent raw traffic — you do **not** need to enable debug logging first.
- If this bug isn't tied to one lock (e.g. setup, login, or config flow problems), download the whole integration's diagnostics instead: Settings → Devices & services → TTLock → the three-dot menu on the integration card → **Download diagnostics**.
- Only if the download above doesn't show the traffic around when the bug happened (for example, Home Assistant has restarted since): enable debug logging, reproduce the issue, and attach the resulting logs. See https://www.home-assistant.io/docs/configuration/troubleshooting/#enabling-debug-logging.
