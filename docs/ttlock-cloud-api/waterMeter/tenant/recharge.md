<!-- Source: https://euopen.ttlock.com/document/doc?urlName=cloud%2FwaterMeter%2Ftenant%2FrechargeEn.html -->

# Tenant recharge

`https://api.sciener.com/v3/waterMeterUser/recharge`

This interface doesn't require accessToken

Tenants use the gateway to top up their accounts; if Bluetooth operation is required, please refer to the SDK for the corresponding application.

Upon successful top-up, you must actively invoke the meter-reading interface to save the latest data to the server.

### 1 Request example

`POST, ContentType:application/x-www-form-urlencoded`

```
curl --location -g --request POST 'https://api.sciener.com/v3/waterMeterUser/recharge'\
--data-urlencode 'clientId=4773aa036f7f49c68d876bb4be85c80c' \
--data-urlencode 'waterMeterId=3001' \
--data-urlencode 'type=2' \
--data-urlencode 'rechargeAmount=100.00' \
--data-urlencode 'rechargeM3=100.00' \
--data-urlencode 'date=1625025703000'
```

### **2 Request parameters**

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| clientId | String | Y | Assigned client\_id from [created application](https://euopen.ttlock.com/CreateApplicationEn) |
| executeToken | String | Y | Token used by tenants when recharge |
| date | Long | Y | Current time (timestamp, in milliseconds) |
| waterMeterId | String | Y | Water Meter ID |
| type | Int | Y | 2- Through the gateway |
| rechargeAmount | Double | Y | Recharge amount |

### 3 Response and example

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
