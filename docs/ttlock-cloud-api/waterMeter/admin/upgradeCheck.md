<!-- Source: https://euopen.ttlock.com/document/doc?urlName=cloud%2FwaterMeter%2Fadmin%2FupgradeCheckEn.html -->

# Check if the water meter firmware needs to be upgraded.

`https://api.sciener.com/v3/waterMeter/upgradeCheck`

### **1 Request example**

`POST, ContentType:application/x-www-form-urlencoded`

```
curl --location -g --request POST 'https://api.sciener.com/v3/waterMeter/upgradeCheck'\
--data-urlencode 'clientId=4773aa036f7f49c68d876bb4be85c80c' \
--data-urlencode 'accessToken=dfd5489d0cee31f0bdfaf59d0d42d71f' \
--data-urlencode 'waterMeterId=3001' \
--data-urlencode 'date=1625025703000'
```

### **2 Request parameters**

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| clientId | String | Y | Assigned client\_id from [created application](https://euopen.ttlock.com/CreateApplicationEn) |
| accessToken | String | Y | Access token， obtain by calling [get access token](https://euopen.ttlock.com/document/doc?urlName=cloud/oauth2/getAccessTokenEn.html) API |
| date | Long | Y | Current time (timestamp, in milliseconds) |
| waterMeterId | String | Y | Water Meter ID |

### 3 Response and example

| Name | Type | Description |
| --- | --- | --- |
| needUpgade | Int | Upgrade required: 0 - No, 1 - Yes, 2 - Unknown |
| firmwareInfo | String | Firmware information |
| firmwarePackage | String | firmware package |
| version | String | Latest firmware version number |

```
{
    "needUpgrade":1,
    "firmwareInfo":{
        "modelNum":"SN774-V01",
        "hardwareRevision":"1.1",
        "firmwareRevision":"1.1.28.250625"
    },
 "firmwarePackage":"LTIwLC03NSwtMTMsLTE0LC0xMiwtMjcsLTE4LC0yNSwtMjksLTIsLTgsLTcsLTM2LC0xNCwtMTgsLTc1LC04MywtNzUsLTkwLC05NSwtOTEsLTkxLC05NCwtOTAsLTkzLC05MCwtOTQsLTgyLC05MywtODEsLTgyLC04OSwtODksLTg5LC03NSwtNjksLTc1LC0zMCwtMjcsLTUsLTc1LC04MywtNzUsLTEsLTI5LC0yOSwtMjUsLTgzLC03MiwtNzIsLTI2LC0yLC03LC0yLC0zMCwtMjUsLTEwLC0xMiwtNCwtMTAsLTE2LC0xNCwtNzEsLTI4LC0xMiwtMiwtMTQsLTcsLTE0LC0yNywtNzEsLTEyLC03LC03MiwtMTUsLTIsLTI3LC02LC0zMiwtMTAsLTI3LC0xNCwtNzIsLTYwLC0zOSwtNTYsLTkxLC05MywtOTEsLTY1LC00NSwtODksLTkwLC01NiwtNjAsLTUwLC0zOSwtNDAsLTY2LC01NiwtNjMsLTk0LC03MSwtOTIsLTcxLC05MiwtNzEsLTkxLC05MCwtODksLTk0LC05MSwtODksLTY1LC04OSwtOTAsLTY2LC03MSwtMTksLTIsLTI1LC03NSwtMjIsNzk=",
    "version":"1.1.28.250626"
}
```
