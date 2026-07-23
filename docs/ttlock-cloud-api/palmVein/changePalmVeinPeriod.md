<!-- Source: https://euopen.ttlock.com/document/doc?urlName=cloud%2FpalmVein%2FchangePalmVeinPeriodEn.html -->

# Change the period of a palm vein

`https://api.sciener.com/v3/palmVein/changePeriod`

### **1 Request method and example**

`POST, ContentType:application/x-www-form-urlencoded`

```
curl --location -g --request POST 'https://api.sciener.com/v3/palmVein/changePeriod'\
--data-urlencode 'clientId=4773aa036f7f49c68d876bb4be85c80c' \
--data-urlencode 'accessToken=dfd5489d0cee31f0bdfaf59d0d42d71f' \
--data-urlencode 'id=66252' \
--data-urlencode 'startDate=1755134678245' \
--data-urlencode 'endDate=1855134678245' \
--data-urlencode 'type=1' \
--data-urlencode 'date=1755134678245'
```

### **2 Request parameters**

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| clientId | String | Y | client\_id from [Create application](https://euopen.sciener.com/CreateApplication) |
| accessToken | String | Y | Access token，refer to: [Get access token](https://euopen.sciener.com/document/doc?urlName=cloud/oauth2/getAccessTokenEn.html) |
| id | Int | Y | id of a palm vein |
| startDate | Long | Y | The time when it becomes valid (timestamp in millisecond) |
| endDate | Long | Y | The time when it is expired (timestamp in millisecond) |
| cyclicConfig | JsonArray | N | Recurring time period, the valid time period of each week day must be the same, for example: `[{"weekDay":1, "startTime":480,"endTime":1080},{"weekDay":2, "startTime":480,"endTime":1080}]` for valid from 8:00 to 18:00 in Monday and Tuesday. |
| type | Int | N | Method: 1-APP, 2-remote synchronous delivery, 4-WiFi wake-up asynchronous delivery; if not transmitted, the default APP is deleted |
| date | Long | Y | Current time (timestamp in millisecond) |

cyclicConfig：

| Parameter | Type | Description |
| --- | --- | --- |
| startTime | Int | The time when it becomes valid, in minute, for example 480 for 8:00 |
| endTime | Int | The time when it becomes validit is expired, in minute, for example 1080 for 18:00 |
| weekDay | Int | Week day：1-7, 1-Monday, 2-Tuesday...7-Sunday |

### 3 Response and example

| Parameter | Type | Description |
| --- | --- | --- |
| errcode | Int | error code |
| errmsg | String | error message |
| description | String | error message in Chinese |

```
{
    "errcode": 0,
    "errmsg": "none error message or means yes",
    "description": "表示成功或是"    
}
```
