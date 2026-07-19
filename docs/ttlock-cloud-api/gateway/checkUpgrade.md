<!-- Source: https://euopen.ttlock.com/document/doc?urlName=cloud%2Fgateway%2FcheckUpgradeEn.html -->

# Gateway upgrade check

**`https://api.sciener.com/v3/gateway/upgradeCheck`**

Check if the gateway have a new version of firmware base on the three parameters: modelNum、hardwareRevision、firmwareRevision uploaded to cloud server when addding gateway.

Refer to [APP SDK DEMO](https://euopen.ttlock.com/document/doc?urlName=appSdkV3/androidSdkDemoV3/downloadEn.html) for detailed steps of gateway firmware upgrade.

### 1 Request example

```
curl --location --request GET 'https://api.sciener.com/v3/gateway/upgradeCheck?clientId=4773aa036f7f49c68d876bb4be85c80c&accessToken=dfd5489d0cee31f0bdfaf59d0d42d71f&gatewayId=78979&date=1626674054000'
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
| needUpgrade | Int | Is upgrading available: 0-No, 1-Yes, 2-Unknown |
| firmwareInfo | String | Firmware info, will be used in APP SDK |
| version | String | Latest firmware version |

```
{
    "needUpgrade": 1, 
    "firmwareInfo": 
    {
        "modelNum":"SN227",
        "hardwareRevision":"1.1.2",
        "firmwareRevision":"1.1.20.1027"
    },
    "version":"1.1.21.1027"
}
```
