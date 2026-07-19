<!-- Source: https://euopen.ttlock.com/document/doc?urlName=cloud%2Fgateway%2FsetIntoUpgradeModeEn.html -->

# Set gateway into upgrade mode

**`https://api.sciener.com/v3/gateway/setUpgradeMode`**

Set gateway into upgrade mode remotely, when a gateway is in upgrade mode, it can't accept commands.

If you want to develop function of gateway firmware upgrade base on our APP SDK, please refer to: [APP SDK DEMO](https://euopen.ttlock.com/document/doc?urlName=appSdkV3/androidSdkDemoV3/downloadEn.html)。

### 1 Request example

`POST, ContentType:application/x-www-form-urlencoded`

```
curl --location -g --request POST 'https://api.sciener.com/v3/gateway/setUpgradeMode' \
--data-urlencode 'clientId=4773aa036f7f49c68d876bb4be85c80c' \
--data-urlencode 'accessToken=dfd5489d0cee31f0bdfaf59d0d42d71f' \
--data-urlencode 'gatewayId=78979' \
--data-urlencode 'date=1625025703000'
```

### 2 Request parameters

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| clientId | String | Y | client\_id from [Create application](https://euopen.ttlock.com/CreateApplication) |
| accessToken | String | Y | Access token，refer to: [Get access token](https://euopen.ttlock.com/document/doc?urlName=cloud/oauth2/getAccessTokenEn.html) |
| gatewayId | Int | Y | Gateway ID |
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
