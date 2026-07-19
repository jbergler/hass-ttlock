# TTLock Cloud API (v3) documentation mirror

Snapshot of the EU open-platform cloud API docs from <https://euopen.ttlock.com/document/doc?urlName=cloud%2FerrorCodeEn.html>, scraped for offline reference. Each page below mirrors one `documentPages/htmlPages/cloud/...` page; the original page for any doc can be reconstructed via `https://euopen.ttlock.com/document/doc?urlName=<urlencoded path>`.

## Regenerating / updating this mirror

The site (`euopen.ttlock.com`) is a client-rendered SPA, so the doc content itself is not reachable by just requesting the page URL you see in the browser (`/document/doc?urlName=...`) — that always returns the app shell, not the article. Two things make scraping it tractable without running a browser:

1. **The full page tree is a static XML nav manifest**: <https://euopen.ttlock.com/documentPages/htmlPages/pathEng/newMenu.xml>. It's a nested `<group>`/`<item>` tree; every leaf `<item>` has a `file="..."` attribute, e.g. `file="cloud/passcode/addEn.html"`. Entries under the `cloud` group are the cloud API v3 pages this directory mirrors (other top-level groups cover the user guide, app SDK, etc. — not scraped here). Some entries are wrapped in HTML comments (`<!-- ... -->`) to hide them from the live nav; skip those, they're pages the vendor deliberately disabled.
2. **Each `file` value maps directly to a static HTML page**, no API calls or JS execution needed: `https://euopen.ttlock.com/documentPages/htmlPages/<file>` (e.g. `.../documentPages/htmlPages/cloud/passcode/addEn.html`). That page is a plain Typora export — the actual content lives in a `<div id="write">`, with everything else being page chrome/CSS to ignore.

So the process is: fetch `newMenu.xml` → parse out `file="cloud/...En.html"` entries (excluding commented-out ones) → fetch `documentPages/htmlPages/<file>` for each → pull out `#write` → convert to markdown.

A couple of scraping-specific gotchas worth knowing before redoing this:
- Code samples are rendered as a full CodeMirror editor widget, not a plain `<pre><code>`. Reconstruct each block's text by concatenating the `.get_text()` of its `<pre class="CodeMirror-line">` children in order — that reproduces the original source (including whitespace) faithfully.
- A few pages embed images as inline `data:image/...;base64,...` `<img src>` — worth decoding and saving those to real image files instead of leaving giant base64 blobs in the markdown.
- Internal cross-links use relative/short hrefs (e.g. `/document/doc?urlName=...`, `/CreateApplication`); resolve them to absolute `https://euopen.ttlock.com/...` URLs so links still work once the content is out of its original page.
- Not every live page is linked from `newMenu.xml` — e.g. `cloud/lock/featureValueEn.html` (mirrored at [lock/featureValue.md](lock/featureValue.md)) is a real page referenced directly from this integration's `models.py` but absent from the nav tree. Worth checking known doc links from the codebase against the scraped set after a refresh.

## Cloud API V3

- [Error code](errorCode.md)

## Cloud API V3 / OAuth

- [Get access token](oauth2/getAccessToken.md)
- [Refresh access token](oauth2/refreshAccessToken.md)

## Cloud API V3 / User APIs

- [User register](user/register.md)
- [Reset password](user/resetPassword.md)
- [Get user list](user/list.md)
- [Delete user](user/delete.md)

## Cloud API V3 / Lock APIs

- [Lock initialize](lock/initialize.md)
- [Get the lock list of an account](lock/list.md)
- [Get lock details](lock/detail.md)
- [Delete lock](lock/delete.md)
- [Update lock data(reset ekey, reset passcode)](lock/updateLockData.md)
- [Change lock name](lock/rename.md)
- [Change the super passcode](lock/updateAdminPasscode.md)
- [Transfer Lock](lock/transfer.md)
- [Upload lock battery](lock/updateElectricQuantity.md)
- [Set auto lock time](lock/setAutoLockTime.md)
- [Config the passage mode of a lock](lock/configurePassageMode.md)
- [Get the passage mode configuration of a lock](lock/getPassageModeConfiguration.md)
- [Set hotel card sector](lock/setHotelCardSector.md)
- [Modify lock settings](lock/updateSetting.md)
- [Query lock settings](lock/querySetting.md)
- [get lock Working hours](lock/getWorkingMode.md)
- [set lock Working hours](lock/configWorkingMode.md)

## Cloud API V3 / Ekey APIs

- [Send ekey](ekey/send.md)
- [Get the eKey list of an account](ekey/list.md)
- [Get one ekey](ekey/get.md)
- [Get ekeys of a lock](ekey/listByLock.md)
- [Delete the ekey](ekey/delete.md)
- [Freeze the ekey](ekey/freeze.md)
- [Unfreeze the ekey](ekey/unfreeze.md)
- [Modify ekey](ekey/modify.md)
- [Change the valid time of the ekey](ekey/updateDate.md)
- [Key authorization](ekey/authorize.md)
- [Cancel key authorization](ekey/unauthorize.md)
- [get eKey Unlocking Link](ekey/getUnlockLink.md)

## Cloud API V3 / Passcode APIs

- [Get a random passcode](passcode/get.md)
- [Add a custom passcode](passcode/add.md)
- [Get all created passcodes of a lock](passcode/list.md)
- [Delete one passcode](passcode/delete.md)
- [Change passcode](passcode/update.md)

## Cloud API V3 / Gateway APIs

- [Unlock](gateway/unlock.md)
- [Lock](gateway/lock.md)
- [Get the open state of a lock](gateway/queryLockOpenState.md)
- [Get lock time](gateway/queryLockDatetime.md)
- [Adjust lock time](gateway/adjustLockDatetime.md)
- [Query lock battery](gateway/queryLockElectricQuantity.md)
- [Get the gateway list of an account](gateway/list.md)
- [Delete gateway](gateway/delete.md)
- [Rename gateway](gateway/rename.md)
- [Transfer Gateway](gateway/transfer.md)
- [Get the gateway list of a lock](gateway/listByLock.md)
- [Get the lock list of a gateway](gateway/listLock.md)
- [Get the device list of a gateway](gateway/listDevice.md)
- [Get gateway detail](gateway/detail.md)
- [Query the init status of the gateway](gateway/isInitSuccess.md)
- [Upload detail info of gateway](gateway/uploadDetail.md)
- [Gateway upgrade check](gateway/checkUpgrade.md)
- [Set gateway into upgrade mode](gateway/setIntoUpgradeMode.md)

## Cloud API V3 / IC Card APIs

- [Add IC card](card/add.md)
- [Get all IC cards of a lock](card/list.md)
- [Delete IC card](card/delete.md)
- [Change the period of validity of IC card](card/update.md)
- [Clear IC card](card/clear.md)
- [Rename IC card](card/rename.md)

## Cloud API V3 / Fingerprint APIs

- [Add fingerprint](fingerprint/add.md)
- [Get the fingerprint list of a lock](fingerprint/list.md)
- [Delete fingerprint](fingerprint/delete.md)
- [Change the period of validity of fingerprint](fingerprint/update.md)
- [Clear fingerprint](fingerprint/clear.md)
- [Rename fingerprint](fingerprint/rename.md)

## Cloud API V3 / Face APIs

- [Get face feature data by photo](face/getFeatureData.md)
- [Add face](face/add.md)
- [Get face list of lock](face/list.md)
- [Delete face data](face/delete.md)
- [Change the period of validity of face](face/update.md)
- [Clear face data](face/clear.md)
- [Rename face](face/rename.md)

## Cloud API V3 / Unlock record APIs

- [Get unlock records](lockRecord/list.md)
- [Upload records](lockRecord/upload.md)
- [Delete records](lockRecord/delete.md)
- [Clear records](lockRecord/clear.md)
- [Lock Records Notify](lockRecord/notify.md)

## Cloud API V3 / Group APIs

- [Add Group](group/add.md)
- [Get the group list of an account](group/list.md)
- [Set the group of a lock](group/setLockGroup.md)
- [Delete Group](group/delete.md)
- [Rename Group](group/rename.md)
- [set a group of a device](group/setDeviceGroup.md)

## Cloud API V3 / Lock upgrade APIs

- [Upgrade check](lockUpgrade/check.md)
- [Upgrade recheck](lockUpgrade/recheck.md)

## Cloud API V3 / Wireless Keyboard APIs

- [Add wireless keypad](wirelessKeypad/add.md)
- [Get wireless keypads of a lock](wirelessKeypad/list.md)
- [Delete wireless keypad](wirelessKeypad/delete.md)
- [Rename wireless keypad](wirelessKeypad/rename.md)
- [Check wireless keypad firmware upgrade](wirelessKeypad/upgrade.md)
- [Check if upgrade was successful](wirelessKeypad/upgradeSuccess.md)

## Cloud API V3 / Remote APIs

- [Add remote](remote/add.md)
- [Get remotes of a lock](remote/list.md)
- [Delete remote](remote/delete.md)
- [Clear remote](remote/clear.md)
- [Update remote](remote/update.md)
- [Check remote firmware upgrade](remote/upgrade.md)
- [Check if upgrade was successful](remote/upgradeSuccess.md)

## Cloud API V3 / Door Sensor APIs / Standalone Door Sensor

- [Add a door sensor](doorSensor/standaloneDoorSensor/add.md)
- [Get door sensor list](doorSensor/standaloneDoorSensor/list.md)
- [Configure Network](doorSensor/standaloneDoorSensor/configNetwork.md)
- [Operation records](doorSensor/standaloneDoorSensor/listRecord.md)
- [Update](doorSensor/standaloneDoorSensor/update.md)
- [Delete](doorSensor/standaloneDoorSensor/delete.md)
- [Add door sensor user](doorSensor/standaloneDoorSensor/addDoorSensorUser.md)
- [Get door sensor user list](doorSensor/standaloneDoorSensor/listDoorSensorUser.md)
- [Delete door sensor user](doorSensor/standaloneDoorSensor/deleteDoorSensorUser.md)
- [Configure alert](doorSensor/standaloneDoorSensor/configAlertFlag.md)
- [Get alert records](doorSensor/standaloneDoorSensor/getAlertFlag.md)
- [Notify records](doorSensor/standaloneDoorSensor/notify.md)

## Cloud API V3 / Door Sensor APIs

- [Add door sensor](doorSensor/add.md)
- [Query the door sensor of a lock](doorSensor/query.md)
- [Delete door sensor](doorSensor/delete.md)
- [Rename door sensor](doorSensor/update.md)
- [Check door sensor firmware upgrade](doorSensor/upgrade.md)
- [Check if upgrade was successful](doorSensor/upgradeSuccess.md)
- [Door sensor opening and closing records](doorSensor/listRecord.md)

## Cloud API V3 / NB-IoT APIs

- [Register NB-IoT Lock](nb/register.md)
- [Get NB-Iot Lock Device Info](nb/getDeviceInfo.md)
- [Get NB-IoT Clound Server Info](nb/getPlatformIpAndPort.md)

## Cloud API V3 / QR code APIs

- [Add QR code](qrCode/add.md)
- [Get all QR codes of a lock](qrCode/list.md)
- [Get QR code Data](qrCode/getData.md)
- [Update QR code](qrCode/update.md)
- [Delete QR code](qrCode/delete.md)
- [Clear QR code](qrCode/clear.md)

## Cloud API V3 / WiFi lock APIs

- [Upload network information](wifiLock/updateNetwork.md)
- [Get detailed information](wifiLock/detail.md)

## Cloud API V3 / Palm Vein APIs

- [Get Palm Vein List](palmVein/getPalmVeinList.md)
- [Add A PalmVein](palmVein/addPalmVein.md)
- [Rename PalmVein](palmVein/renamePalmVein.md)
- [Change Palm Vein Period](palmVein/changePalmVeinPeriod.md)
- [Delete A Palm Vein](palmVein/deletePalmVein.md)
- [Clear Palm Vein](palmVein/clearPalmVein.md)

## Cloud API V3 / Electricity meter / Admin APIs

- [Get list of electricity meters](eletricMeter/admin/list.md)
- [Electricity meter binding](eletricMeter/admin/bind.md)
- [Administrator obtains meter details](eletricMeter/admin/detail.md)
- [Meter reading](eletricMeter/admin/syncState.md)
- [Administrator recharge](eletricMeter/admin/recharge.md)
- [Modify unit price](eletricMeter/admin/updatePrice.md)
- [Turn the power meter on/off](eletricMeter/admin/controlOnOff.md)
- [Clear the electricity meter](eletricMeter/admin/clear.md)
- [Change the name of the electricity meter](eletricMeter/admin/rename.md)
- [Operation log list](eletricMeter/admin/listOperationRecords.md)
- [Electricity usage record list](eletricMeter/admin/listRecords.md)
- [Set maximum power](eletricMeter/admin/updateMaxPower.md)
- [Set payment mode](eletricMeter/admin/updatePayMode.md)
- [Check if the meter firmware needs to be upgraded.](eletricMeter/admin/upgradeCheck.md)
- [Upgrade successful](eletricMeter/admin/upgradeSuccess.md)
- [Gateway associated with the electricity meter](eletricMeter/admin/gatewayList.md)
- [Generate an executeToken that can be used for top-ups.](eletricMeter/admin/createExecuteToken.md)
- [Bluetooth broadcast meter reading](eletricMeter/admin/decodeBleAdvertising.md)

## Cloud API V3 / Electricity meter / Tenant APIs

- [Tenants obtain electricity meter details](eletricMeter/tenant/detail.md)
- [Meter reading by tenant](eletricMeter/tenant/syncState.md)
- [Tenant recharge](eletricMeter/tenant/recharge.md)
- [Tenant query operation record](eletricMeter/tenant/listRecharge.md)
- [Tenant can check electricity usage records list](eletricMeter/tenant/listRecords.md)

## Cloud API V3 / Water Meter / Admin APIs

- [Get water meter list](waterMeter/admin/list.md)
- [Binding water meter](waterMeter/admin/bind.md)
- [Administrator obtains water meter details](waterMeter/admin/detail.md)
- [Meter reading](waterMeter/admin/syncState.md)
- [Administrator recharge](waterMeter/admin/recharge.md)
- [Modify unit price](waterMeter/admin/updatePrice.md)
- [Turn the water meter on/off](waterMeter/admin/controlOnOff.md)
- [Clear water meter](waterMeter/admin/clear.md)
- [Change water meter name](waterMeter/admin/rename.md)
- [Operation log list](waterMeter/admin/listOperationRecords.md)
- [Water usage record list](waterMeter/admin/listRecords.md)
- [Set total water consumption](waterMeter/admin/updateTotal.md)
- [Set payment mode](waterMeter/admin/updatePayMode.md)
- [Check if the water meter firmware needs to be upgraded.](waterMeter/admin/upgradeCheck.md)
- [Upgrade successful](waterMeter/admin/upgradeSuccess.md)
- [Gateway associated with the water meter](waterMeter/admin/gatewayList.md)
- [Generate an executeToken that can be used for top-ups.](waterMeter/admin/createExecuteToken.md)
- [Bluetooth broadcast meter reading](waterMeter/admin/decodeBleAdvertising.md)

## Cloud API V3 / Water Meter / Tenant APIs

- [Get water meter details](waterMeter/tenant/detail.md)
- [Meter reading by tenant](waterMeter/tenant/syncState.md)
- [Tenant top-up](waterMeter/tenant/recharge.md)
- [Tenant query operation record](waterMeter/tenant/listRecharge.md)
- [Tenant can check the list of water usage records](waterMeter/tenant/listRecords.md)

## Cloud API V3 / Lock APIs

- [Lock feature value (bitmask)](lock/featureValue.md)
