<!-- Source: https://euopen.ttlock.com/document/doc?urlName=cloud%2FwaterMeter%2Fadmin%2FlistEn.html -->

# Get water meter list

`https://api.sciener.com/v3/waterMeter/list`

This interface retrieves water meters added by users in the TechHero app, or water meters initialized via the initialization lock interface through the app SDK.

For water meters added in the TechHero app, please use the app's username and password used to add the water meter to obtain an access token before calling this interface.

For water meters initialized via the cloud-based initialization lock interface, please use the same access token used when initializing the water meter.

### **1 Request example**

`POST, ContentType:application/x-www-form-urlencoded`

```
curl --location -g --request POST 'https://api.sciener.com/v3/waterMeter/list'\
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
| groupId | Int | N | Water Meter ID |
| searchStr | String | N | Search criteria include searching by meter name and meter number/water meter ID list. |

### 3 Response and example

| Name | Type | Description |
| --- | --- | --- |
| list | JSONObject | List |

Objects in the list

| Name | Type | Description |
| --- | --- | --- |
| waterMeterId | Int | Water Meter ID |
| online | Int | Online status: 0 - No 1 - Yes |
| rssi | Int | Signal strength |
| onOff | Int | Water supply status: 0 - No 1 - Yes 2 - Unknown |
| name | String | Water meter name |
| number | String | Water meter number |
| payMode | Int | Operating modes: 0 - Energy metering mode; 1 - Prepaid mode |
| totalM3 | String | Total water consumption |
| remainderM3 | String | Remaining water volume |
| price | String | Unit price |
| electricQuantity | Int | Battery |

```
{
   "list": [
        {
            "number": "EM2_005012",
            "payMode": 1,
            "price": "10.00",
            "name": "EM2_005012",
            "online": 0,
            "waterMeterId": 270,
            "remainderM3": "0.00",
            "onOff": 1,
            "totalM3": "0.00"
        }
    ]
}
```
