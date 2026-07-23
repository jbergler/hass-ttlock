<!-- Source: https://euopen.ttlock.com/document/doc?urlName=cloud%2Fekey%2FupdateDateEn.html -->

# Change the valid time of the ekey

**`https://api.sciener.com/v3/key/changePeriod`**

### 1 Request example

`POST, ContentType:application/x-www-form-urlencoded`

```
curl --location -g --request POST 'https://api.sciener.com/v3/key/changePeriod' \
--data-urlencode 'clientId=4773aa036f7f49c68d876bb4be85c80c' \
--data-urlencode 'accessToken=dfd5489d0cee31f0bdfaf59d0d42d71f' \
--data-urlencode 'keyId=27619' \
--data-urlencode 'startDate=1625025703000' \
--data-urlencode 'endDate=1635025703000' \
--data-urlencode 'date=1625025703000'
```

### 2 Request parameters

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| clientId | String | Y | client\_id from [Create application](https://euopen.ttlock.com/CreateApplication) |
| accessToken | String | Y | Access token，refer to: [Get access token](https://euopen.ttlock.com/document/doc?urlName=cloud/oauth2/getAccessTokenEn.html) |
| keyId | Int | Y | Ekey ID |
| startDate | Long | Y | The time when it becomes valid (timestamp in millisecond) |
| endDate | Long | Y | The time when it is expired (timestamp in millisecond) |
| date | Long | Y | Current time (timestamp in millisecond) |

### 3 Response and example

| Parameter | Type | Description |
| --- | --- | --- |
| errcode | Int | Error code |
| errmsg | String | Error message |

```
{
    "errcode": 0, 
    "errmsg": "none error message"
}
```
