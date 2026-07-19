<!-- Source: https://euopen.ttlock.com/document/doc?urlName=cloud%2FdoorSensor%2FupgradeEn.html -->

# Check Doorsensor Firmware Upgrade

**`https://api.sciener.com/v3/doorSensor/upgradeCheck`**

This API is used to request firmware upgrade information for multiple door sensors.
If the server does not have the relevant data, it will return "Unknown". You need to call the relevant API in the app SDK to obtain the latest product model, hardware version number, and firmware version number, and then call this API again.

### 1 Request example

`POST, ContentType:application/x-www-form-urlencoded`

```
curl --location -g --request POST 'https://api.sciener.com/v3/doorSensor/upgradeCheck' \
--data-urlencode 'clientId=4773aa036f7f49c68d876bb4be85c80c' \
--data-urlencode 'accessToken=dfd5489d0cee31f0bdfaf59d0d42d71f' \
--data-urlencode 'doorSensorId=3001' \
--data-urlencode 'date=1625025703000'
```

### 2 Request parameters

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| clientId | String | Y | client\_id from [Create application](https://euopen.ttlock.com/CreateApplication) |
| accessToken | String | Y | access Toke，refer to [get access token](https://euopen.ttlock.com/document/doc?urlName=cloud/oauth2/getAccessToken.html) |
| doorSensorId | Int | Y | door sensor ID |
| modelNum | String | N | product model |
| hardwareRevision | String | N | hardware version number |
| firmwareRevision | String | N | firmware version number |
| date | Long | Y | current time (timestamp in millisecond) |

### 3 Response and example

| Parameters | Type | Description |
| --- | --- | --- |
| needUpgrade | Int | Is upgrading available: 0 - No, 1 - Yes, 2 - Unknown |
| firmwareInfo | String | firmware information |
| firmwarePackage | String | firmware package |
| version | String | latest firmware version number |

```
{
    "needUpgrade":1,
    "firmwareInfo":{
        "modelNum":"SN405_LR",
        "hardwareRevision":"1.1",
        "firmwareRevision":"1.0.0.210101"
    },
 "firmwarePackage":"LTIwLC03NSwtMTMsLTE0LC0xMiwtMjcsLTE4LC0yNSwtMjksLTIsLTgsLTcsLTM2LC0xNCwtMTgsLTc1LC04MywtNzUsLTkwLC05NSwtOTEsLTkxLC05NCwtOTAsLTkzLC05MCwtOTQsLTgyLC05MywtODEsLTgyLC04OSwtODksLTg5LC03NSwtNjksLTc1LC0zMCwtMjcsLTUsLTc1LC04MywtNzUsLTEsLTI5LC0yOSwtMjUsLTgzLC03MiwtNzIsLTI2LC0yLC03LC0yLC0zMCwtMjUsLTEwLC0xMiwtNCwtMTAsLTE2LC0xNCwtNzEsLTI4LC0xMiwtMiwtMTQsLTcsLTE0LC0yNywtNzEsLTEyLC03LC03MiwtMTUsLTIsLTI3LC02LC0zMiwtMTAsLTI3LC0xNCwtNzIsLTYwLC0zOSwtNTYsLTkxLC05MywtOTEsLTY1LC00NSwtODksLTkwLC01NiwtNjAsLTUwLC0zOSwtNDAsLTY2LC01NiwtNjMsLTk0LC03MSwtOTIsLTcxLC05MiwtNzEsLTkxLC05MCwtODksLTk0LC05MSwtODksLTY1LC04OSwtOTAsLTY2LC03MSwtMTksLTIsLTI1LC03NSwtMjIsNzk=",
    "version":"1.0.0.211223"
}
```
