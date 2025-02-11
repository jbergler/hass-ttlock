# hass-ttlock

Home Assistant integration for TTLock based locks.

[<img src="https://img.shields.io/github/v/release/jbergler/hass-ttlock?style=for-the-badge" />](https://github.com/jbergler/hass-ttlock/releases/latest)
<picture><img src="https://img.shields.io/github/downloads/jbergler/hass-ttlock/total?style=for-the-badge" /></picture>
<picture><img src="https://img.shields.io/github/downloads/jbergler/hass-ttlock/latest/total?style=for-the-badge" /></picture>
[<img src="https://img.shields.io/codecov/c/github/jbergler/hass-ttlock?style=for-the-badge&token=EJI902TDWE" />](https://app.codecov.io/gh/jbergler/hass-ttlock)

## Overview

This integration uses the TTLock Cloud to communicate with your lock. It supports the following features:

- Locking and unlocking
- Discovery of locks on startup
- Real-time updates via a webhook (no periodic polling which wastes battery)
- Additional sensors for battery, last operator + reason
- Add new pass codes
- Delete expired pass codes
- List passcodes
- List records history (lock, unlock etc)

## Known working locks

> If this integration is working for you, please leave a comment [here](https://github.com/jbergler/hass-ttlock/issues/1)

- [Catchface Smart Lock](https://s.click.aliexpress.com/e/_DFtl1Wd)
- [Cielsa Dawn Fingerprint Digital Doorknob](https://www.amazon.com/dp/B09B557YZK)
- [E-Lok 9 series](https://www.e-lok.com/9-series)
- [Raykube locks](https://raukube0001.aliexpress.com/store/1864009/search?SearchText=TT+Lock)
- [Simpled locks](https://simpled.uk/)
- [TOTOWISER locks](https://www.amazon.com.au/gp/product/B08TQKW3JC)
- [YRHAND locks](https://yrhandlock.com)

# Usage

## Requirements

1. A TTLock based smart lock
1. A Gateway (if your lock doesn't have integrated wifi)
   - These can be purchased from the vendor of your lock or direct from [Aliexpress](https://s.click.aliexpress.com/e/_DEPpClx)
1. Remote unlock must be enabled for each lock
   - This must be done while in bluetooth range of the lock, from the mobile app
   - Here is a [youtube video](https://www.youtube.com/watch?v=ni-38QpoNA4) which explains the process

## Creating an OAuth APP

1. Go to https://open.ttlock.com/manager and create an account
1. Register an application (this will take a few days to get approved)
1. Install the extension [via HACS](https://my.home-assistant.io/redirect/hacs_repository/?owner=jbergler&repository=hass-ttlock&category=integration) and restart Home Assistant
1. Setup the integration [via Home Assistant UI](https://my.home-assistant.io/redirect/config_flow_start/?domain=ttlock)
   - The first credentials you will be prompted for are the Application Client ID & Secret that you created earlier.
   - The second credentials you will be prompted for are the username/password you use to login to the ttlock app on your phone.
1. Once the integration is working you should receive a system notification with the webhook url
   - Go back to https://open.ttlock.com/manager
   - Select your application, and edit the "callback url". Enter the webhook url from the notification
   - Test by unlocking your door
   - If the event data was received by home assistant the notification will go away indicating that everything is working.

# Troubleshooting

## Common issues

1. Invalid client_id
   - Your Application (ie oauth) Client ID & Client Secret for the application you created on open.ttlock.com.
   - These are stored in the "Application Credentials" feature of Home Assistant. If you need to remove/update them, please follow the official [docs](https://www.home-assistant.io/integrations/application_credentials)
   - If you get this error, you need to remove the invalid credentials and re-setup the integration with the correct ones.
1. Invalid username or password
   - The username/password for open.ttlock.com is only used for managing API credentials for the ttlock cloud - do not use these within home assistant.
   - The username/password for the ttlock (or 3rd party branded) mobile app. This is the account that will work.
1. "Failed to execute the action lock/lock." or "The function is not supported for this lock"
   - This is most likely because you haven't enabled remote unlock, please follow the instructions in the requirements section.

## Reporting issues

When reporting issues, please attach the diagnostic information and consider enabling debug logging to provide extra information.

## Development

You can find all the TTLock API calls here https://euopen.ttlock.com/document

# Say thanks

If you found this helpful and you'd like to say thanks you can do so via buy me a coffee or a beer.
I've put a bunch of time into this integration and it always puts a smile on my face when people say thanks!

<a href="https://www.buymeacoffee.com/jbergler" target="_blank"><img src="https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png" height="60" width="217"></a>
