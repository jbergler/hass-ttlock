<!-- Source: https://euopen.ttlock.com/document/doc?urlName=cloud%2Fgateway%2FuploadDetailEn.html -->

# Upload detail info of gateway

**`https://api.sciener.com/v3/gateway/uploadDetail`**

Upload the gateway's firmware version info and network name to the cloud server after the [gateway is successfully initialized](https://euopen.ttlock.com/document/doc?urlName=cloud/gateway/isInitSuccessEn.html).

### 1 Request example

`POST, ContentType:application/x-www-form-urlencoded`

```
curl --location -g --request POST 'https://api.sciener.com/v3/gateway/uploadDetail' \
--data-urlencode 'clientId=4773aa036f7f49c68d876bb4be85c80c' \
--data-urlencode 'accessToken=dfd5489d0cee31f0bdfaf59d0d42d71f' \
--data-urlencode 'gatewayId=5678' \
--data-urlencode 'modelNum=SN227' \
--data-urlencode 'hardwareRevision=1.1.2' \
--data-urlencode 'firmwareRevision=1.2.21.0524' \
--data-urlencode 'networkName=1-101-WIFI' \
--data-urlencode 'date=1625025703000'
```

### 2 Request parameters

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| clientId | String | Y | client\_id from [Create application](https://euopen.ttlock.com/CreateApplication) |
| accessToken | String | Y | Access token，refer to: [Get access token](https://euopen.ttlock.com/document/doc?urlName=cloud/oauth2/getAccessTokenEn.html) |
| gatewayId | Int | Y | Gateway ID, returned by: [Gateway init](https://euopen.ttlock.com/document/doc?urlName=cloud/gateway/isInitSuccessEn.html) |
| modelNum | String | Y | Product model |
| hardwareRevision | String | Y | Hardware version |
| firmwareRevision | String | Y | Firmware version |
| networkName | String | Y | The network name gateway connected to |
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
