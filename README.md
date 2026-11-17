# hass-ttlock

Home Assistant integration for TTLock-based locks. It uses the TTLock cloud for setup. It can also read and control locks directly over Bluetooth. It can do the same for a Wi-Fi lock over your local network. After setup you can turn the cloud off.

[<img src="https://img.shields.io/github/v/release/jbergler/hass-ttlock?style=for-the-badge" />](https://github.com/jbergler/hass-ttlock/releases/latest)
<picture><img src="https://img.shields.io/github/downloads/jbergler/hass-ttlock/total?style=for-the-badge" /></picture>
<picture><img src="https://img.shields.io/github/downloads/jbergler/hass-ttlock/latest/total?style=for-the-badge" /></picture>
[<img src="https://img.shields.io/codecov/c/github/jbergler/hass-ttlock?style=for-the-badge&token=EJI902TDWE" />](https://app.codecov.io/gh/jbergler/hass-ttlock)
[<img src="https://img.shields.io/badge/buy_me_a_coffee-say_thanks-FFDD00?style=for-the-badge&logo=buymeacoffee&logoColor=black" />](https://www.buymeacoffee.com/jbergler)

## Features

- Lock and unlock through a TTLock gateway or the lock's built-in Wi-Fi
- Read lock, battery, and supported door state over Home Assistant's Bluetooth
- Lock and unlock a lock over Bluetooth (opt-in)
- Read, lock, and unlock a Wi-Fi lock on your local network, with the cloud turned off (opt-in)
- Choose the order in which Bluetooth, the WiFi proxy, and the cloud are tried
- Turn the TTLock cloud off, and start from saved data when the cloud cannot be reached
- Use an ESPHome Bluetooth proxy when the lock is not close to Home Assistant
- Receive near-real-time updates through a TTLock webhook
- Use locks shared from another TTLock account
- See gateway status, signal strength, and how each lock is connected
- Manage passage mode, autolock, passcodes, IC cards, and fingerprints
- View lock records, the last operator, and the last trigger

Locking and unlocking use the routes you allow, in the order you choose. Without Bluetooth control or local Wi-Fi control, they need a working cloud route through a TTLock gateway or the lock's built-in Wi-Fi.

## How updates reach Home Assistant

| Method | What it does | When it runs |
|---|---|---|
| Webhook | Sends lock events from TTLock to Home Assistant | Near real time when the callback URL works. Off while the cloud is off |
| Cloud polling | Checks lock state and refreshes lock details | State every 30 minutes and details every 6 hours by default. Only while the cloud is on |
| Bluetooth | Reads lock, battery, and supported door state directly | During state checks, plus every 5 minutes when the cloud is off or cannot reach the lock |
| WiFi proxy | Reads lock and battery over the lock's own connection to Home Assistant | During state checks, soon after the lock reports an event, and every 5 minutes while the cloud is off. Only after Home Assistant takes the lock over |

The polling intervals and the connection options are under **Settings > Devices & services > TTLock > Configure**. State checks run at the poll interval with the cloud on or off. With the cloud off, they use only Bluetooth and the WiFi proxy. Webhooks are still the fastest update path for locks with a gateway or built-in Wi-Fi.

## What you need

| Goal | What you need |
|---|---|
| Set up the integration | A TTLock account and an approved TTLock Developer application |
| Lock or unlock through the cloud | A TTLock gateway or built-in Wi-Fi, with Remote unlock enabled |
| Get near-real-time updates | A webhook URL that TTLock can reach from the public internet |
| Read state over Bluetooth | A Home Assistant Bluetooth adapter or ESPHome Bluetooth proxy in range |
| Read a lock without a gateway or Wi-Fi | Bluetooth range; updates are scheduled, not instant |
| Lock or unlock over Bluetooth | Bluetooth range, **Control locks over Bluetooth** turned on, and owner or authorized access |
| Control a Wi-Fi lock locally | **Accept WiFi locks redirected to Home Assistant** turned on, a router that can redirect the lock's outbound TCP port 4999 to Home Assistant, the cloud turned off, and owner or authorized access |
| Run without the TTLock cloud | Bluetooth control or local Wi-Fi control, and one setup with the cloud so the lock list and keys are saved |

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
- Home Assistant reads a lock every 5 minutes when the cloud is off or cannot reach the lock. A manual turn may take that long to appear in Home Assistant.
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
| **Accept WiFi locks redirected to Home Assistant** | Listen on TCP port 4999 for this account's Wi-Fi locks. See [Local Wi-Fi control](#local-wi-fi-control) | Off |
| **Transport order** | The order to try Bluetooth, the WiFi proxy, and the cloud. You can choose any of the six orders | Bluetooth, then WiFi proxy, then cloud |
| **Use the TTLock cloud** | Use the cloud for control, state, details, and webhooks | On |

The options form refuses these combinations:

- **Control locks over Bluetooth** without **Read locks over Bluetooth**.
- **Use the TTLock cloud** off, with neither **Control locks over Bluetooth** nor **Accept WiFi locks redirected to Home Assistant** on.
- **Use the TTLock cloud** off, when every saved lock on the account is shared with you without admin rights.

### Transport order

Lock and unlock try each route that is available now, in your order. They stop at the first route that succeeds. If a route fails, they try the next route.

- Bluetooth is available when **Control locks over Bluetooth** is on and the lock is in range.
- The WiFi proxy is available when Home Assistant has taken the lock over. It is also available when a lock that was taken over recently is reconnecting. Home Assistant then waits up to 20 seconds for the lock to come back.
- The cloud is available when **Use the TTLock cloud** is on.

If no route is available, lock and unlock fail with an error. If every route tried fails, they fail with an error that names those routes.

State reads try Bluetooth and the WiFi proxy in your order, then the cloud when it is on.

### Cloud off

With **Use the TTLock cloud** off:

- Home Assistant makes no calls to the TTLock cloud. The one exception is an entry with no saved lock list. It asks the cloud for the list once, at startup.
- The locks start from the saved lock list, keys, and details.
- Lock and unlock use only Bluetooth and the WiFi proxy.
- Home Assistant takes over a Wi-Fi lock that is redirected to it, and controls the lock locally. The lock is then offline to the TTLock cloud, so the TTLock app loses remote control of it.
- Home Assistant unregisters the webhook, so the TTLock cloud cannot send it events. If another entry that uses the same Developer application still has the cloud on, the webhook stays for that entry. Home Assistant keeps a Nabu Casa cloudhook, so the webhook URL does not change.
- Every action except `ttlock.update_state` needs the cloud, so it fails with an error. Home Assistant does not create the autolock and lock sound switches.
- Home Assistant does not refresh lock details, keys, gateways, or the door-sensor battery from the cloud. If you change a lock in the TTLock app, turn the cloud on again to refresh its saved data.
- Lock state updates after each action from Home Assistant, and at each poll. A lock that Bluetooth or the WiFi proxy can reach also updates every 5 minutes. A taken-over Wi-Fi lock also updates soon after it reports an event.

Turning the cloud back on reloads the integration and registers the webhook again.

### Starting without the cloud

If the integration cannot reach the TTLock cloud at startup, it starts the locks from the saved data. It also raises a repair issue. The issue clears when the cloud answers again. If TTLock refused the credentials, the integration also asks you to sign in again. Bluetooth and the WiFi proxy keep working in the meantime. The first setup always needs the cloud.

## Local Wi-Fi control

A Wi-Fi TTLock lock keeps one outbound connection to the TTLock cloud, `wifilock.ttlock.com`, on TCP port 4999. If your network sends that connection to Home Assistant instead, the integration can see the lock. With the cloud off, it controls the lock locally.

1. Turn on **Accept WiFi locks redirected to Home Assistant** under **Configure**. Home Assistant then listens on TCP port 4999 on all interfaces. If Home Assistant runs in a container, publish or forward that port. If the port is already in use, a repair issue tells you.
2. Make port 4999 on Home Assistant reachable from the network the lock is on.
3. On your router, add a destination NAT rule: TCP from the lock's address with destination port 4999, sent to Home Assistant's address on port 4999. Scope the rule to the lock's address. A rule that also catches Home Assistant's own outbound traffic sends its cloud relay back to itself.
4. Make the lock reconnect. It picks up the rule on its next connection, which happens every few minutes. To make it reconnect at once, restart the lock or clear the router's connection state.
5. Check the lock's **WiFi-local Link** sensor. It shows `passthrough` while the cloud is on and `takeover` once you turn **Use the TTLock cloud** off.
6. To control the lock locally, turn **Use the TTLock cloud** off. While the cloud is on, Home Assistant only relays the lock to the cloud, and lock and unlock still go through the cloud.

While the cloud is on, Home Assistant takes the lock over locally if it cannot reach the TTLock cloud for the relay.

If two config entries include the same lock, the entry loaded last handles it, and the lock reconnects to it. Unloading that entry hands the lock back to the other one.

### How Home Assistant checks a connection

- Home Assistant only accepts locks from your TTLock account, matched by MAC address. A MAC address is not a secret, so keep port 4999 reachable only from the lock's network.
- When Home Assistant holds the lock's key, the connection must prove it has the key. With the cloud off, the lock must answer the handshake with its exact one-block success reply. With the cloud on, Home Assistant forwards only the cloud's first command. It holds back the rest of the cloud's traffic until the lock answers that command in the same way.
- A reconnecting lock can send frames it held for its old connection. Home Assistant drops up to 3 frames before the login and up to 3 before the reply. It does not answer or relay them. A fourth wrong frame ends the connection, with nothing sent back. A missing reply after a dropped frame does the same. Only a connection that has proved the key can replace the lock's current session.
- After that, a taken-over lock may send only what the lock sends. That is the reply to Home Assistant's current command, its heartbeat, its events, and the state reports it sends unasked. The lock sends such a report after a lock or unlock. Anything else ends the session, with nothing sent back. Home Assistant ignores a reply that arrives after its command timed out, unless it has sent another command since.
- A failed proof and a session ended by an unexpected frame both count as failures. Three failures from one address within 10 minutes block that address for 30 seconds. Each further failure blocks it again for twice as long, up to 15 minutes. An address is forgotten 15 minutes after its last failure or block. A successful connection does not clear it.
- Home Assistant holds at most 2 unproven connections from one address and 4 in total. A new connection closes the oldest one. Each handshake step must finish within 15 seconds.
- With debug logging on, the log shows `skipped a 0x.. frame before the login` or `before the key proof` for each dropped frame. It shows `session for lock ... ended (HandshakeRejected)` for a failed proof. Once the address is blocked, it shows `refused a connection from ...`.
- The lock's reply to the handshake never changes. Anyone who has seen it on the network can replay it and hold a session as if they were the lock. This is a limit of the lock's protocol. The checks above and the blocks make it impractical for such a session to learn the lock's key.

Optionally, block the lock from the internet at your firewall, for both IPv4 and IPv6, so it can only reach Home Assistant. The cloud-on relay still works, because Home Assistant makes the onward connection to the cloud.

### If the cloud hostname stops resolving

The router rule catches the connection after the lock looks up `wifilock.ttlock.com`. If that name ever stops resolving, the lock cannot find any server, and the rule has nothing to catch. In that case, add a DNS override for `wifilock.ttlock.com` that answers only the lock's network. The lock may not use your local DNS server, so check that the override reaches it.

Do not make the override global. Home Assistant also looks up that name for the cloud relay. If the name resolves to Home Assistant itself, Home Assistant refuses to relay to itself and takes the lock over locally instead.

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
- Autolock and lock sound, for owner or authorized access, while the cloud is on
- Gateway status, while the cloud is on
- Gateway Signal and Bluetooth Signal diagnostic sensors, disabled by default

Connection diagnostic sensors stay available when nothing can reach the lock, so they can show why:

| Sensor | What it shows | When it appears |
|---|---|---|
| Connection | The first route in your transport order that can reach the lock now (`ble`, `wifi_local`, or `cloud`), or unknown. Attributes show whether lock and unlock would avoid the cloud, the route they would use, the route the last action used, and the state of each route | Always |
| WiFi-local Link | `down`, `passthrough`, `takeover`, or `reconnecting`. The `last_seen` attribute shows when the lock last sent traffic | **Accept WiFi locks redirected to Home Assistant** on |
| Cloud Online | Whether the TTLock cloud can reach the lock. Off while the cloud is off | Always |
| Bluetooth Reachable | Whether Home Assistant's Bluetooth can reach the lock | **Read locks over Bluetooth** on |

## Actions

Actions are under **Developer tools > Actions**. Some management actions require owner or authorized-admin access.

- Configure passage mode and autolock
- Force a state refresh with `ttlock.update_state`
- Create, list, modify, delete, and clean up passcodes
- List lock records
- List, add, rename, and delete IC cards
- List, rename, and delete fingerprints

Every action except `ttlock.update_state` goes through the TTLock cloud. With the cloud off, it fails with an error.

Calling `ttlock.update_state` often can use a large part of the TTLock API allowance when Bluetooth is unavailable. Do not run it every few seconds.

### Known limitations

- Local Wi-Fi control needs the cloud off. While the cloud is on, the WiFi proxy only relays the lock to the cloud.

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

The lock is not Connectable. The TTLock cloud cannot reach it now, and neither can Bluetooth or the WiFi proxy. Check the gateway status, the Connection and WiFi-local Link sensors, and the disabled-by-default Gateway Signal and Bluetooth Signal sensors.

### Changes appear only after a poll

Open **Configure**, copy the webhook URL, and make sure it matches the Callback URL in the TTLock Developer portal. Check that TTLock can reach it without passing through a login page. With the cloud off there is no webhook, so this is expected.

### Lock or unlock fails

For `-4043`, **The function is not supported for this lock**, or a read-only control, enable Remote unlock in the TTLock mobile app while standing near the lock. Check it again after replacing or re-adding a lock.

For a Shared lock, the owner must also enable Remote unlock for your Ekey.

The error names the routes it tried. Check the Connection sensor's `preferred_control_transport` attribute. If it is empty, no route can lock or unlock the lock now. For the WiFi proxy, the WiFi-local Link sensor must show `takeover`. For Bluetooth, **Control locks over Bluetooth** must be on and the lock must be in range.

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
