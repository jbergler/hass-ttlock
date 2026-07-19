<!-- Source: https://euopen.ttlock.com/document/doc?urlName=cloud%2FwaterMeter%2Fadmin%2FdecodeBleAdvertisingEn.html -->

# Bluetooth broadcast meter reading

`https://api.sciener.com/v3/waterMeter/decodeBleAdvertising`

This interface supports the meter reading function by reporting water meter data through the broadcast reading function in the APP SDK.

### **1 Request example**

`POST, ContentType:application/x-www-form-urlencoded`

```
curl --location -g --request POST 'https://api.sciener.com/v3/waterMeter/decodeBleAdvertising'\
--data-urlencode 'clientId=4773aa036f7f49c68d876bb4be85c80c' \
--data-urlencode 'accessToken=dfd5489d0cee31f0bdfaf59d0d42d71f' \
--data-urlencode 'mac=11:22:33:44:55:66' \
--data-urlencode 'executeResponse=11140100000000280003E80100000064815300445878' \
--data-urlencode 'date=1625025703000'
```

### **2 Request parameters**

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| clientId | String | Y | Assigned client\_id from [created application](https://euopen.ttlock.com/CreateApplicationEn) |
| accessToken | String | Y | Access token， obtain by calling [get access token](https://euopen.ttlock.com/document/doc?urlName=cloud/oauth2/getAccessTokenEn.html) API |
| date | Long | Y | Current time (timestamp, in milliseconds) |
| mac | String | Y | Water Meter MAC |
| executeResponse | Int | Y | Broadcast data obtained from the SDK |

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
