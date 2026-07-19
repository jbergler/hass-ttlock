<!-- Source: https://euopen.ttlock.com/document/doc?urlName=cloud%2Fgateway%2FisInitSuccessEn.html -->

# Query the init status of the gateway

**`https://api.sciener.com/v3/gateway/isInitSuccess`**

Request this API after you [calling the APP SDK method to add a gateway](https://euopen.ttlock.com/document/doc?urlName=appSdkV3/androidSdkDemoV3/androidExample/gatewayEn.html)(**in 3 minutes**), to check whether it has been added successfully, it's success if gatewayId is returned.

If there is a new gateway with the same `gatewayNetMac` added to your account in the last 3 minutes, this API will return it's gateway ID.

**Notice：If you have not named the gateway with it's `gatewayNetMac` when init the gateway by APP SDK，request this API with parameter `gatewayNetMac` set to your specific name.**

### 1 Request example

`POST, ContentType:application/x-www-form-urlencoded`

```
curl --location -g --request POST 'https://api.sciener.com/v3/gateway/isInitSuccess' \
--data-urlencode 'clientId=4773aa036f7f49c68d876bb4be85c80c' \
--data-urlencode 'accessToken=dfd5489d0cee31f0bdfaf59d0d42d71f' \
--data-urlencode 'gatewayNetMac=C1:30:E7:F4:97:17' \
--data-urlencode 'date=1625025703000'
```

### 2 Request parameters

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| clientId | String | Y | client\_id from [Create application](https://euopen.ttlock.com/CreateApplication) |
| accessToken | String | Y | Access token，refer to: [Get access token](https://euopen.ttlock.com/document/doc?urlName=cloud/oauth2/getAccessTokenEn.html) |
| gatewayNetMac | String | Y | The Mac which you will get when calling the APP SDK method to add a gateway |
| date | Long | Y | Current time (timestamp in millisecond) |

### 3 Response and example

| Parameter | Type | Description |
| --- | --- | --- |
| gatewayId | Int | Gateway ID |

```
{
    "gatewayId": 5678, 
}
```
