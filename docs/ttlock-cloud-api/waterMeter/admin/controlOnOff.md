<!-- Source: https://euopen.ttlock.com/document/doc?urlName=cloud%2FwaterMeter%2Fadmin%2FcontrolOnOffEn.html -->

# Operate the water meter to turn the water on/off

`https://api.sciener.com/v3/waterMeter/controlOnOff`

If you need to use a gateway to turn the water on and off via Bluetooth, please refer to the corresponding app's SDK.

### **1 Request example**

`POST, ContentType:application/x-www-form-urlencoded`

```
curl --location -g --request POST 'https://api.sciener.com/v3/waterMeter/controlOnOff'\
--data-urlencode 'clientId=4773aa036f7f49c68d876bb4be85c80c' \
--data-urlencode 'accessToken=dfd5489d0cee31f0bdfaf59d0d42d71f' \
--data-urlencode 'waterMeterId=3001' \
--data-urlencode 'onOff=1' \
--data-urlencode 'date=1625025703000'
```

### **2 Request parameters**

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| clientId | String | Y | Assigned client\_id from [created application](https://euopen.ttlock.com/CreateApplicationEn) |
| accessToken | String | Y | Access token， obtain by calling [get access token](https://euopen.ttlock.com/document/doc?urlName=cloud/oauth2/getAccessTokenEn.html) API |
| date | Long | Y | Current time (timestamp, in milliseconds) |
| waterMeterId | String | Y | Water Meter ID |
| onOff | Int | Y | Water supply status: 0 - No 1 - Yes |

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
