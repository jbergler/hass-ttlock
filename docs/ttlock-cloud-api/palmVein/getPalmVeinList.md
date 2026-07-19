<!-- Source: https://euopen.ttlock.com/document/doc?urlName=cloud%2FpalmVein%2FgetPalmVeinListEn.html -->

# Get Palm vein list of a lock

`https://api.sciener.com/v3/palmVein/list`

### **1 Request method and example**

`POST, ContentType:application/x-www-form-urlencoded`

```
curl --location -g --request POST 'https://api.sciener.com/v3/palmVein/list'\
--data-urlencode 'clientId=4773aa036f7f49c68d876bb4be85c80c' \
--data-urlencode 'accessToken=dfd5489d0cee31f0bdfaf59d0d42d71f' \
--data-urlencode 'lockId=3466252' \
--data-urlencode 'pageNo=1' \
--data-urlencode 'pageSize=10' \
--data-urlencode 'date=1625025703000'
```

### **2 Request parameters**

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| clientId | String | Y | client\_id from [Create application](https://euopen.sciener.com/CreateApplication) |
| accessToken | String | Y | Access token，refer to: [Get access token](https://euopen.sciener.com/document/doc?urlName=cloud/oauth2/getAccessTokenEn.html) |
| lockId | Int | Y | id of a lock |
| searchStr | String | N | Search string (name fuzzy matching) |
| pageNo | Int | Y | Page no, start from 1 |
| pageSize | Int | Y | Items per page, max 200 |
| date | Long | Y | Current time (timestamp in millisecond) |

### 3 Response and example

| Parameter | Type | Description |
| --- | --- | --- |
| id | Int | id of a palm vein |
| name | String | name of a palm vein |
| number | String | number of a palm vein |
| status | String | Status: 1-normal, 2-invalid, 3-pending |
| nickname | String | nickname of creator |
| startDate | Long | The time when it becomes valid (timestamp in millisecond) |
| endDate | Long | The time when it is expired (timestamp in millisecond) |
| type | Int | Type：1-Normal、4-Recurring |
| cyclicConfig | JsonArray | Recurring time period, the valid time period of each week day must be the same, for example: `[{"weekDay":1, "startTime":480,"endTime":1080},{"weekDay":2, "startTime":480,"endTime":1080}]` for valid from 8:00 to 18:00 in Monday and Tuesday. |
| createDate | Long | Creation time |

cyclicConfig：

| Parameter | Type | Description |
| --- | --- | --- |
| startTime | Int | The time when it becomes valid, in minute, for example 480 for 8:00 |
| endTime | Int | The time when it becomes validit is expired, in minute, for example 1080 for 18:00 |
| weekDay | Int | Week day：1-7, 1-Monday, 2-Tuesday...7-Sunday |

```
{
    "list": [
        {
            "id": 34242,
            "type": 1,
            "number":50432139657219,
            "name": "Test",
            "startDate": 1625025703000,
            "endDate": 1665025703000,
            "status": 1,
            "createDate": 1625025703000,
            "nickname": "landlord2",
        }
    ],
    "pageNo":1,
    "pageSize":10,
    "pages":1,
    "total":1
}
```
