<!-- Source: https://euopen.ttlock.com/document/doc?urlName=cloud%2FeletricMeter%2Fadmin%2FrenameEn.html -->

# Change the name of the electricity meter

`https://api.sciener.com/v3/electricMeter/rename`

### **1 Request example**

`POST, ContentType:application/x-www-form-urlencoded`

```
curl --location -g --request POST 'https://api.sciener.com/v3/electricMeter/rename'\
--data-urlencode 'clientId=4773aa036f7f49c68d876bb4be85c80c' \
--data-urlencode 'accessToken=dfd5489d0cee31f0bdfaf59d0d42d71f' \
--data-urlencode 'electricMeterId=3001' \
--data-urlencode 'name=电表名称2' \
--data-urlencode 'date=1625025703000'
```

### **2 Request parameters**

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| clientId | String | Y | Assigned client\_id from [created application](https://euopen.ttlock.com/CreateApplicationEn) |
| accessToken | String | Y | Access token， obtain by calling [get access token](https://euopen.ttlock.com/document/doc?urlName=cloud/oauth2/getAccessTokenEn.html) API |
| date | Long | Y | Current time (timestamp, in milliseconds) |
| electricMeterId | String | Y | Electricity meter ID |
| name | String | Y | Electricity meter name |

### **3 Response and example**

| Name | Type | Description |
| --- | --- | --- |
| errcode | Int | Error code |
| errmsg | String | Error message |
| description | String | Error Description |

```
{
    "errcode": 0,
    "errmsg": "none error message or means yes",
    "description": "表示成功或是"    
}
```
