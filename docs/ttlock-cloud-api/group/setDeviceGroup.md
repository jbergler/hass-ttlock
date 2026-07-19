<!-- Source: https://euopen.ttlock.com/document/doc?urlName=cloud%2Fgroup%2FsetDeviceGroupEn.html -->

# Set a Group of a Device

**`https://api.sciener.com/v3/group/setDeviceGroup`**

For locks, the lock administrator calls this interface to set the lock's group, and ordinary ekey users call this interface to set the key's group.

For water and electricity meters, only the water and electricity meter administrator calls this interface to set the meter's group.

### 1 Request example

`POST, ContentType:application/x-www-form-urlencoded`

```
curl --location -g --request POST 'https://api.sciener.com/v3/group/setDeviceGroup' \
--data-urlencode 'clientId=4773aa036f7f49c68d876bb4be85c80c' \
--data-urlencode 'accessToken=dfd5489d0cee31f0bdfaf59d0d42d71f' \
--data-urlencode 'deviceType=0' \
--data-urlencode 'deviceId=3442324' \
--data-urlencode 'groupId=1342' \
--data-urlencode 'date=1625025703000'
```

### 2 Request parameters

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| clientId | String | Y | client\_id from [Create application](https://euopen.ttlock.com/CreateApplication) |
| accessToken | String | Y | Access token，refer to: [Get access token](https://euopen.ttlock.com/document/doc?urlName=cloud/oauth2/getAccessTokenEn.html) |
| deviceType | String | Y | Equipment type 0: Lock. 1: Electricity meter. 2: Water meter. |
| deviceId | String | Y | Device ID |
| groupId | String | Y | Group ID, returned by the "[Get Group List](https://euopen.ttlock.com/document/doc?urlName=cloud/group/listEn.html)" interface; passing 0 cancels the group. |
| date | Long | Y | Current time (timestamp in millisecond) |

### 3 Response and example

| Parameter | Type | Description |
| --- | --- | --- |
| errcode | Int | Error code |
| errmsg | String | Error message |
| description | String | Error description |

```
{
    "errcode": 0, 
    "errmsg": "none error message",
    "description":"表示成功或是"
}
```
