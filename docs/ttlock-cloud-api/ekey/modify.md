<!-- Source: https://euopen.ttlock.com/document/doc?urlName=cloud%2Fekey%2FmodifyEn.html -->

# Modify ekey

**`https://api.sciener.com/v3/key/update`**

Administrator or authorized Administrator can use this API to modify the ekey name and if can remote unlock

### 1 Request example

`POST, ContentType:application/x-www-form-urlencoded`

```
curl --location -g --request POST 'https://api.sciener.com/v3/key/update' \
--data-urlencode 'clientId=4773aa036f7f49c68d876bb4be85c80c' \
--data-urlencode 'accessToken=dfd5489d0cee31f0bdfaf59d0d42d71f' \
--data-urlencode 'keyId=27619' \
--data-urlencode 'keyName=ekey3' \
--data-urlencode 'remoteEnable=2' \
--data-urlencode 'date=1625025703000'
```

### 2 Request parameters

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| clientId | String | Y | client\_id from [Create application](https://euopen.ttlock.com/CreateApplication) |
| accessToken | String | Y | Access token，refer to: [Get access token](https://euopen.ttlock.com/document/doc?urlName=cloud/oauth2/getAccessTokenEn.html) |
| keyId | Int | Y | Ekey ID |
| keyName | string | N | Ekey name will be modifed |
| remoteEnable | Int | N | Is remote unlock enabled: 1-yes, 2-no |
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
