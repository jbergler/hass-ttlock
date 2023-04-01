# hass-ttlock

Home Assistant integration for TTLock based locks.

## Currently supported

- Locking and unlocking
- Loading status on startup

## Limitations (will be fixed over time)

- Almost everything else
- Notably, state is currently not being refreshed automatically so the locked/unlocked status of the lock will not update.

# Usage

## Creating an OAuth APP

1. Go to https://open.ttlock.com/manager and create an account
1. Register an application (this will take a few days to get approved)
1. Install the extension via HACS
   - You will need to add add custom repository 'jbergler/hass-ttlock' of type 'integration'
1. Setup the integration via Home Assistant UI

## Reporting issues

Please enable debug logging with the following config and share all logs matching 'ttlock'

```
logger:
  default: info
  logs:
    homeassistant.components.ttlock: debug
```
