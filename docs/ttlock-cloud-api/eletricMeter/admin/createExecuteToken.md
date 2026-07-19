<!-- Source: https://euopen.ttlock.com/document/doc?urlName=cloud%2FeletricMeter%2Fadmin%2FcreateExecuteTokenEn.html -->

# Generate an executeToken that can be used for top-ups.

`https://api.sciener.com/v3/electricMeter/createExecuteToken`

Before both gateway top-up and APP SDK Bluetooth top-up, you need to request this interface to obtain the executeToken.

Then, pass the obtained executeToken as a parameter to either the gateway top-up or the APP SDK Bluetooth top-up.

### **1 Request example**

`POST, ContentType:application/x-www-form-urlencoded`

```
curl --location -g --request POST 'https://api.sciener.com/v3/electricMeter/createExecuteToken'\
--data-urlencode 'clientId=4773aa036f7f49c68d876bb4be85c80c' \
--data-urlencode 'accessToken=dfd5489d0cee31f0bdfaf59d0d42d71f' \
--data-urlencode 'electricMeterId=3001' \
--data-urlencode 'rechargeKwh=100' \
--data-urlencode 'rechargeAmount=100' \
--data-urlencode 'date=1625025703000'
```

### **2 Request parameters**

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| clientId | String | Y | Assigned client\_id from [created application](https://euopen.ttlock.com/CreateApplicationEn) |
| accessToken | String | Y | Access token， obtain by calling [get access token](https://euopen.ttlock.com/document/doc?urlName=cloud/oauth2/getAccessTokenEn.html) API |
| date | Long | Y | Current time (timestamp, in milliseconds) |
| electricMeterId | String | Y | Electricity meter ID |
| rechargeKwh | Double | Y | Recharge amount |
| rechargeAmount | String | Y | Recharge amount |

### **3 Response and example**

| Name | Type | Description |
| --- | --- | --- |
| executeToken | String | executeToken |

```
{
    "executeToken": 11223344
}
```
