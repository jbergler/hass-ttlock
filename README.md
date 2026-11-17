# hass-ttlock

Home Assistant integration for TTLock-based locks. It uses the TTLock cloud for setup and control, and can also read and control locks directly over Bluetooth.

[<img src="https://img.shields.io/github/v/release/jbergler/hass-ttlock?style=for-the-badge" />](https://github.com/jbergler/hass-ttlock/releases/latest)
<picture><img src="https://img.shields.io/github/downloads/jbergler/hass-ttlock/total?style=for-the-badge" /></picture>
<picture><img src="https://img.shields.io/github/downloads/jbergler/hass-ttlock/latest/total?style=for-the-badge" /></picture>
[<img src="https://img.shields.io/codecov/c/github/jbergler/hass-ttlock?style=for-the-badge&token=EJI902TDWE" />](https://app.codecov.io/gh/jbergler/hass-ttlock)
[<img src="https://img.shields.io/badge/buy_me_a_coffee-say_thanks-FFDD00?style=for-the-badge&logo=buymeacoffee&logoColor=black" />](https://www.buymeacoffee.com/jbergler)

## Features

- Lock and unlock through a TTLock gateway or the lock's built-in Wi-Fi
- Read lock, battery, and supported door state over Home Assistant's Bluetooth
- Lock and unlock a lock over Bluetooth (opt-in)
- Choose whether Bluetooth or the cloud is tried first
- Start from saved data when the cloud cannot be reached
- Use an ESPHome Bluetooth proxy when the lock is not close to Home Assistant
- Receive near-real-time updates through a TTLock webhook
- Use locks shared from another TTLock account
- See gateway status, signal strength, and how each lock is connected
- Manage passage mode, autolock, passcodes, IC cards, and fingerprints
- View lock records, the last operator, and the last trigger

Locking and unlocking use the routes you allow, in the order you choose. Without Bluetooth control, they need a working cloud route through a TTLock gateway or the lock's built-in Wi-Fi.

## How updates reach Home Assistant

| Method | What it does | When it runs |
|---|---|---|
| Webhook | Sends lock events from TTLock to Home Assistant | Near real time when the callback URL works |
| Cloud polling | Checks lock state and refreshes lock details | State every 30 minutes and details every 6 hours by default |
| Bluetooth | Reads lock, battery, and supported door state directly | During state checks, plus every 5 minutes for a lock with no cloud route |

The polling intervals and the connection options are under **Settings > Devices & services > TTLock > Configure**. Webhooks are still the fastest update path for locks with a gateway or built-in Wi-Fi.

## What you need

| Goal | What you need |
|---|---|
| Set up the integration | A TTLock account and an approved TTLock Developer application |
| Lock or unlock through the cloud | A TTLock gateway or built-in Wi-Fi, with Remote unlock enabled |
| Get near-real-time updates | A webhook URL that TTLock can reach from the public internet |
| Read state over Bluetooth | A Home Assistant Bluetooth adapter or ESPHome Bluetooth proxy in range |
| Read a lock without a gateway or Wi-Fi | Bluetooth range; updates are scheduled, not instant |
| Lock or unlock over Bluetooth | Bluetooth range, **Control locks over Bluetooth** turned on, and owner or authorized access |

## Installation and setup

### Create a TTLock Developer application

TTLock has separate EU/International and China clouds. Your TTLock account and Developer application must use the same cloud.

| Region | Developer portal |
|---|---|
| EU / International | [euopen.ttlock.com](https://euopen.ttlock.com/manager) |
| China | [cnopen.ttlock.com](https://cnopen.ttlock.com/manager) |

Accounts, Developer applications, and locks are not shared between these clouds. Using EU and China entries at the same time in one Home Assistant installation is not currently documented as supported.

1. Open the Developer portal for your region and create an account.
2. Register an application. Approval can take a few days.
3. Install this integration [through HACS](https://my.home-assistant.io/redirect/hacs_repository/?owner=jbergler&repository=hass-ttlock&category=integration), then restart Home Assistant.
4. [Add the TTLock integration](https://my.home-assistant.io/redirect/config_flow_start/?domain=ttlock).
5. Enter the Client ID and Client Secret from your Developer application.
6. Choose your region, then enter the username and password from the TTLock mobile app. Do not use your Developer portal login here.
7. Copy the webhook URL shown during setup into the **Callback URL** field of your Developer application.
8. Lock or unlock a door. The webhook repair notice will clear after Home Assistant receives an event.

The webhook URL must be reachable from the public internet. It cannot be behind a VPN login, HTTP authentication, Cloudflare Access, or another login page. If you protect Home Assistant with a reverse proxy, allow the exact `/api/webhook/...` path without authentication.

You can find the webhook URL again under **Settings > Devices & services > TTLock > Configure**. Locks still update by polling if the webhook is not working.

TTLock allows one callback URL per Developer application. If several TTLock accounts use the same Client ID, this integration shares one webhook URL between them.

### Enable Remote unlock

Remote unlock must be enabled for each lock before Home Assistant can lock or unlock it through the TTLock cloud.

1. Stand near the lock with the TTLock mobile app.
2. Open the lock settings and enable **Remote unlock**.

Replacing or re-adding a lock can turn this setting off. [This video](https://www.youtube.com/watch?v=ni-38QpoNA4) shows the process.

## Bluetooth

Local Bluetooth is enabled by default when Home Assistant's [Bluetooth integration](https://www.home-assistant.io/integrations/bluetooth/) is set up. It also works through ESPHome Bluetooth proxies.

The integration gets the lock's keys from the TTLock cloud. It does not need a second mobile-app login. A successful read reports lock, battery, and supported door state.

- Bluetooth can lock and unlock the lock when **Control locks over Bluetooth** is turned on under **Configure**. It is off by default.
- Home Assistant saves the keys. Bluetooth keeps working through a TTLock cloud outage, and after a restart during one.
- The `member` Access level cannot read the keys. It gets no Bluetooth reads or control.
- A lock with a gateway or built-in Wi-Fi keeps using webhook updates. Bluetooth handles the state check locally when it can.
- Home Assistant reads a lock every 5 minutes when the cloud cannot reach the lock. A manual turn may take that long to appear in Home Assistant.
- The Bluetooth Signal sensor shows whether the lock is in range. Around -80 dBm and below, connections can be slow or fail.
- Bluetooth can be turned off under **Configure** by clearing **Read locks over Bluetooth**.

## Configuration options

These options are under **Settings > Devices & services > TTLock > Configure**. They apply to every lock on the account. A change reloads the integration.

| Option | What it does | Default |
|---|---|---|
| **Poll interval** | How often to check whether each lock is locked, in minutes. From 5 to 1440 | 30 |
| **Detail refresh interval** | How often to refresh slow-changing detail from the TTLock cloud, in hours. From 1 to 168 | 6 |
| **Read locks over Bluetooth** | Read lock state from a lock in Bluetooth range | On |
| **Control locks over Bluetooth** | Lock and unlock a lock in Bluetooth range | Off |
| **Transport order** | The order to try Bluetooth and the cloud | Bluetooth, then cloud |

The options form refuses **Control locks over Bluetooth** without **Read locks over Bluetooth**.

### Transport order

Lock and unlock try each route that is available now, in your order. They stop at the first route that succeeds. If a route fails, they try the next route.

- Bluetooth is available when **Control locks over Bluetooth** is on and the lock is in range.
- The cloud is always available.

If every route tried fails, lock and unlock fail with an error that names those routes.

State reads try Bluetooth first, then the cloud.

### Starting without the cloud

If the integration cannot reach the TTLock cloud at startup, it starts the locks from the saved data. It also raises a repair issue. The issue clears when the cloud answers again. If TTLock refused the credentials, the integration also asks you to sign in again. Bluetooth keeps working in the meantime. The first setup always needs the cloud.

## Shared locks

The integration discovers owned locks and Shared locks. TTLock represents each account's access to a lock as an Ekey.

- The `owner` and `authorized` Access levels can use management actions.
- The `member` Access level can read state. It can lock or unlock only when the owner enabled Remote unlock for its Ekey.
- A frozen, expired, or not-yet-valid Ekey cannot control the lock.

## Entities

Entities appear only when the lock supports the feature and your setup can use it.

- Lock control and state
- Lock battery and door-sensor battery
- Door state
- Last operator and last trigger
- Passage mode
- Autolock and lock sound
- Gateway status
- Gateway Signal and Bluetooth Signal diagnostic sensors, disabled by default

Connection diagnostic sensors stay available when nothing can reach the lock, so they can show why:

| Sensor | What it shows | When it appears |
|---|---|---|
| Connection | The first route in your transport order that can reach the lock now (`ble` or `cloud`), or unknown. Attributes show whether lock and unlock would avoid the cloud, the route they would use, the route the last action used, and the state of each route | Always |
| Cloud Online | Whether the TTLock cloud can reach the lock | Always |
| Bluetooth Reachable | Whether Home Assistant's Bluetooth can reach the lock | **Read locks over Bluetooth** on |

## Actions

Actions are under **Developer tools > Actions**. Some management actions require owner or authorized-admin access.

- Configure passage mode and autolock
- Force a state refresh with `ttlock.update_state`
- Create, list, modify, delete, and clean up passcodes
- List lock records
- List, add, rename, and delete IC cards
- List, rename, and delete fingerprints

Calling `ttlock.update_state` often can use a large part of the TTLock API allowance when Bluetooth is unavailable. Do not run it every few seconds.

## Community-reported locks

These locks have worked for at least one user. A report does not mean every feature works on every firmware version.

If your lock works, please add its model, firmware, connection method, and tested features to [issue #1](https://github.com/jbergler/hass-ttlock/issues/1).

- [Catchface Smart Lock](https://s.click.aliexpress.com/e/_DFtl1Wd)
- [Cielsa Dawn Fingerprint Digital Doorknob](https://www.amazon.com/dp/B09B557YZK)
- [E-Lok 9 series](https://www.e-lok.com/9-series)
- [Raykube locks](https://raukube0001.aliexpress.com/store/1864009/search?SearchText=TT+Lock)
- [Simpled locks](https://simpled.uk/)
- [TOTOWISER locks](https://www.amazon.com.au/gp/product/B08TQKW3JC)
- [YRHAND locks](https://yrhandlock.com)

## Troubleshooting

Start by updating hass-ttlock to the latest release and restarting Home Assistant. If the problem remains, download diagnostics from the affected TTLock device before changing or removing anything.

### No lock appears after setup

1. Check that the selected region matches the TTLock account and Developer application.
2. Check whether the lock is owned by the account or shared with it.
3. Open the integration or device diagnostics and check the gateway, Wi-Fi, and Bluetooth connection details.
4. For a gateway-paired lock, restart the gateway and reload the integration.
5. If TTLock still reports no gateway, remove and re-add the lock in the TTLock app only after saving diagnostics.

### A lock is unavailable

The lock is not Connectable. The TTLock cloud cannot reach it now, and neither can Bluetooth. Check the gateway status, the Connection sensor, and the disabled-by-default Gateway Signal and Bluetooth Signal sensors.

### Changes appear only after a poll

Open **Configure**, copy the webhook URL, and make sure it matches the Callback URL in the TTLock Developer portal. Check that TTLock can reach it without passing through a login page.

### Lock or unlock fails

For `-4043`, **The function is not supported for this lock**, or a read-only control, enable Remote unlock in the TTLock mobile app while standing near the lock. Check it again after replacing or re-adding a lock.

For a Shared lock, the owner must also enable Remote unlock for your Ekey.

The error names the routes it tried. Check the Connection sensor's `preferred_control_transport` attribute. For Bluetooth, **Control locks over Bluetooth** must be on and the lock must be in range.

### Invalid Client ID

Use the Client ID and Client Secret from the TTLock Developer portal. Home Assistant stores them under [Application Credentials](https://www.home-assistant.io/integrations/application_credentials). Remove the invalid credentials there, then add the integration again.

### Invalid username or password

Use the username and password from the TTLock or branded mobile app. Do not use the Developer portal login.

### The battery differs from the TTLock app

Home Assistant uses the battery value from the latest state read. The TTLock app or lock details may show an older cached value.

### TTLock reports a date or time error

Check the Home Assistant host's time, timezone, and NTP setup before changing credentials.

### A removed lock or gateway remains in Home Assistant

After it no longer has any entities and is gone from the TTLock account, remove the old device from its Home Assistant device page.

## Reporting issues

Before opening an issue:

1. Update to the latest hass-ttlock release.
2. Reproduce the problem.
3. Download diagnostics for the affected lock. If no lock device exists, download the integration diagnostics.
4. Include the Home Assistant version, integration version, exact action, and exact error.
5. Review the diagnostics file before posting it publicly, especially if it came from an older integration version.

Diagnostics include recent TTLock traffic and connection details. You do not need to turn on debug logging first.

## Development

TTLock Cloud API documentation:

- [EU / International](https://euopen.ttlock.com/document)
- [China](https://cnopen.ttlock.com/document)

## Say thanks

This integration is built by me and a growing list of [contributors](https://github.com/jbergler/hass-ttlock/graphs/contributors). Thank you to everyone who has sent a fix, tested a lock, or written a useful bug report.

If it has made your setup better and you would like to say thanks, you can [buy me a coffee](https://www.buymeacoffee.com/jbergler).

[![Buy me a coffee](https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png)](https://www.buymeacoffee.com/jbergler)
