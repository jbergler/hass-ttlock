<!-- Source: https://euopen.ttlock.com/document/doc?urlName=cloud%2FeletricMeter%2Fadmin%2FgatewayListEn.html -->

# Gateway associated with the electricity meter

`https://api.sciener.com/v3/electricMeter/gatewayList`

If the meter administrator is also the gateway administrator, the interface will return the gateways near the meter.

### **1 Request example**

`POST, ContentType:application/x-www-form-urlencoded`

```
curl --location -g --request POST 'https://api.sciener.com/v3/electricMeter/gatewayList'\
--data-urlencode 'clientId=4773aa036f7f49c68d876bb4be85c80c' \
--data-urlencode 'accessToken=dfd5489d0cee31f0bdfaf59d0d42d71f' \
--data-urlencode 'electricMeterId=3001' \
--data-urlencode 'date=1625025703000'
```

### **2 Request parameters**

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| clientId | String | Y | Assigned client\_id from [created application](https://euopen.ttlock.com/CreateApplicationEn) |
| accessToken | String | Y | Access token， obtain by calling [get access token](https://euopen.ttlock.com/document/doc?urlName=cloud/oauth2/getAccessTokenEn.html) API |
| date | Long | Y | Current time (timestamp, in milliseconds) |
| electricMeterId | String | Y | Electricity meter ID |

### **3 Response and example**

| Name | Type | Description |
| --- | --- | --- |
| list | JSONObject | List |

Objects in the list

| Name | Type | Description |
| --- | --- | --- |
| rssi | Int | Signal strength between the gateway and the lock, according to the following standards: greater than -75 is strong, greater than -85 is medium, less than -75 is weak. |
| gatewayName | String | Gateway Name |
| gatewayVersion | int | Gateway version |

```
{
    "list": [
        {
            "rssi": -65,
            "gatewayName": "Gateway",
            "gatewayVersion": 2
        }
    ]
}
```
