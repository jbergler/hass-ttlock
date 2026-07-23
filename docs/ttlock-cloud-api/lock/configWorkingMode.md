<!-- Source: https://euopen.ttlock.com/document/doc?urlName=cloud%2Flock%2FconfigWorkingModeEn.html -->

# Configure Working Mode

**`https://api.sciener.com/v3/lock/configWorkingMode`**

**During working hours:** The lock can be unlocked using ekeys, passwords, and other methods.

**During non-working hours:** The lock automatically enters freeze mode, allowing only mechanical keys to be used, and all other unlocking methods are disabled.

Modify the lock's operating hours, including: 1: All-day operation, 2: All-day off, 3: Custom, etc.

You can modify the lock's operating hours via the App SDK and then call this API to synchronize the settings to the cloud, or you can remotely modify the lock's operating hours directly via a gateway or WiFi lock.

The modified settings can be queried using the [Get Working Mode](https://euopen.ttlock.com/document/doc?urlName=cloud/lock/getWorkingModeEn.html) API.

### 1 Request example

`POST, ContentType:application/x-www-form-urlencoded`

```
curl --location -g --request POST 'https://api.sciener.com/v3/lock/configWorkingMode' \
--data-urlencode 'clientId=4773aa036f7f49c68d876bb4be85c80c' \
--data-urlencode 'accessToken=dfd5489d0cee31f0bdfaf59d0d42d71f' \
--data-urlencode 'lockId=3001' \
--data-urlencode 'workingMode=3' \
--data-urlencode 'type=1' \
--data-urlencode 'cyclicConfig=[{"isAllDay":2,"startTime":480,"endTime":1080,"weekDays":[1,2]}]' \
--data-urlencode 'date=1625025703000'
```

### 2 Request parameters

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| clientId | String | Y | client\_id from [Create application](https://euopen.ttlock.com/CreateApplication) |
| accessToken | String | Y | Access token，refer to: [Get access token](https://euopen.ttlock.com/document/doc?urlName=cloud/oauth2/getAccessTokenEn.html) |
| lockId | Int | Y | Lock ID |
| workingMode | String | Y | Working status, 1: working all day, 2: not working all day, 3: custom |
| type | String | Y | Method: 1-via phone bluetooth，should call APP SDK method to add card first. 2-via [gateway](https://euopen.ttlock.com/document/doc?urlName=userGuide/gatewayEn.html) or [WiFi lock](https://euopen.ttlock.com/document/doc?urlName=userGuide/wifiLockEn.html)，you can call this api with type = 2 directly if it's a WiFi lock or it's connected to gateway. The default value is 1. |
| cyclicConfig | String | N | Time period configuration |
| date | Long | Y | Current time (timestamp in millisecond) |

cyclicConfig：

| Parameter | Type | Description |
| --- | --- | --- |
| isAllDay | Int | Whether it is always open all day: 1-Yes, 2-No |
| startTime | Int | Start time in minutes, e.g. 480 means 8:00 |
| endTime | Int | End time in minutes, e.g. 1080 means 18:00 |
| weekDays | JsonArray[Int] | Cycle validity period: 1-7, such as [1,2,3] |

### 3 Response and example

| Parameter | Type | Description |
| --- | --- | --- |
| errcode | Int | Error code |
| errmsg | String | Error message |

```
{
    "errcode": 0, 
    "errmsg": "none error message",
}
```
