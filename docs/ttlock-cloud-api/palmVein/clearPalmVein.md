<!-- Source: https://euopen.ttlock.com/document/doc?urlName=cloud%2FpalmVein%2FclearPalmVeinEn.html -->

# Clear Palm Vein

`https://api.sciener.com/v3/palmVein/clear`

### **1 Request method and example**

`POST, ContentType:application/x-www-form-urlencoded`

```
curl --location -g --request POST 'https://api.sciener.com/v3/palmVein/clear'\
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
| date | Long | Y | Current time (timestamp in millisecond) |

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
