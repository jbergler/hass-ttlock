<!-- Source: https://euopen.ttlock.com/document/doc?urlName=cloud%2FeletricMeter%2Ftenant%2FsyncStateEn.html -->

# **Meter reading by tenant**

`https://api.sciener.com/v3/electricMeterUser/syncState`

Tenants use the gateway to read meters. If Bluetooth operation is required, please refer to the corresponding APP's SDK.

This interface doesn't require accessToken

### **1 Request example**

`POST, ContentType:application/x-www-form-urlencoded`

```
curl --location -g --request POST 'https://api.sciener.com/v3/electricMeterUser/syncState'\
--data-urlencode 'clientId=4773aa036f7f49c68d876bb4be85c80c' \
--data-urlencode 'electricMeterId=3001' \
--data-urlencode 'type=2' \
--data-urlencode 'date=1625025703000'
```

### **2 Request parameters**

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| clientId | String | Y | Assigned client\_id from [created application](https://euopen.ttlock.com/CreateApplicationEn) |
| date | Long | Y | Current time (timestamp, in milliseconds) |
| electricMeterId | String | Y | Electricity meter ID |
| type | Int | Y | 2- Through the gateway |

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
