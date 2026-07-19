<!-- Source: https://euopen.ttlock.com/document/doc?urlName=cloud%2FeletricMeter%2Fadmin%2FlistEn.html -->

# Get list of electricity meters

`https://api.sciener.com/v3/electricMeter/list`

This interface retrieves electricity meters added by the user in the TechHero app, or electricity meters initialized via the initialization lock interface through the app SDK.

For electricity meters added in the TechHero app, please use the app account and password used to add the meter to obtain an access token before calling this interface.

For electricity meters initialized via the cloud-based initialization lock interface, please use the access token of the same user used when initializing the meter.

### **1 Request example**

`POST, ContentType:application/x-www-form-urlencoded`

```
curl --location -g --request POST 'https://api.sciener.com/v3/electricMeter/list'\
--data-urlencode 'clientId=4773aa036f7f49c68d876bb4be85c80c' \
--data-urlencode 'accessToken=dfd5489d0cee31f0bdfaf59d0d42d71f' \
--data-urlencode 'date=1625025703000'
```

### **2 Request parameters**

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| clientId | String | Y | Assigned client\_id from [created application](https://euopen.ttlock.com/CreateApplicationEn) |
| accessToken | String | Y | Access token， obtain by calling [get access token](https://euopen.ttlock.com/document/doc?urlName=cloud/oauth2/getAccessTokenEn.html) API |
| date | Long | Y | Current time (timestamp, in milliseconds) |
| groupId | Int | N | Group ID |
| searchStr | String | N | Search criteria include searching by meter name and a list of meter IDs. |

### **3 Response and example**

| Name | Type | Description |
| --- | --- | --- |
| list | JSONObject | List |

Objects in the list

| Name | Type | Description |
| --- | --- | --- |
| electricMeterId | Int | Electricity meter ID |
| online | Int | Online status: 0 - No 1 - Yes |
| rssi | Int | Signal strength |
| onOff | Int | Power-on status: 0 - No 1 - Yes 2 - Unknown |
| name | String | Electricity meter name |
| number | String | Electricity meter number |
| payMode | Int | Operating modes: 0 - Energy metering mode; 1 - Prepaid mode |
| totalKwh | String | Total electricity consumption |
| remainderKwh | String | Remaining battery power |
| price | String | unit price |

```
{
   "list": [
        {
            "number": "EM2_005012",
            "payMode": 1,
            "price": "10.00",
            "name": "EM2_005012",
            "online": 0,
            "electricMeterId": 270,
            "remainderKwh": "0.00",
            "onOff": 1,
            "totalKwh": "0.00"
        }
    ]
}
```
