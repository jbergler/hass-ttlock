<!-- Source: https://euopen.ttlock.com/document/doc?urlName=cloud%2Flock%2FgetWorkingModeEn.html -->

# Get Working Mode

**`https://api.sciener.com/v3/lock/getWorkingMode`**

**During working hours:** The lock can be unlocked using ekeys, passwords, and other methods.

**During non-working hours:** The lock automatically enters freeze mode, allowing only mechanical keys to be used, and all other unlocking methods are disabled.

### 1 Request example

`POST, ContentType:application/x-www-form-urlencoded`

```
curl --location -g --request POST 'https://api.sciener.com/v3/lock/getWorkingMode' \
--data-urlencode 'clientId=4773aa036f7f49c68d876bb4be85c80c' \
--data-urlencode 'accessToken=dfd5489d0cee31f0bdfaf59d0d42d71f' \
--data-urlencode 'lockId=3001' \
--data-urlencode 'date=1625025703000'
```

### 2 Request parameters

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| clientId | String | Y | client\_id from [Create application](https://euopen.ttlock.com/CreateApplication) |
| accessToken | String | Y | Access token，refer to: [Get access token](https://euopen.ttlock.com/document/doc?urlName=cloud/oauth2/getAccessTokenEn.html) |
| lockId | Int | Y | Lock ID |
| date | Long | Y | Current time (timestamp in millisecond) |

### 3 Response and example

| Parameter | Type | Description |
| --- | --- | --- |
| workingMode | Int | Working status, 1: working all day, 2: not working all day, 3: custom |
| cyclicConfig | String | Working hours |

cyclicConfig中的参数：

| Parameter | Type | Description |
| --- | --- | --- |
| isAllDay | Int | Whether it is always open all day: 1-Yes, 2-No |
| startTime | Int | Start time in minutes, e.g. 480 means 8:00 |
| endTime | Int | End time in minutes, e.g. 1080 means 18:00 |
| weekDays | JsonArray[Int] | Cycle validity period: 1-7, such as [1,2,3] |

```
{
     "workingMode": 3, 
     "cyclicConfig":[{"isAllDay":2,"startTime":480,"endTime":1080,"weekDays":[1,2]}]
}
```
