<!-- Source: https://euopen.ttlock.com/document/doc?urlName=cloud%2FeletricMeter%2Fadmin%2FdetailEn.html -->

# Administrator obtains meter details

`https://api.sciener.com/v3/electricMeter/detail`

### **1 Request example**

`POST, ContentType:application/x-www-form-urlencoded`

```
curl --location -g --request POST 'https://api.sciener.com/v3/electricMeter/detail'\
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
| electricMeterId | Int | Y | Electricity meter ID |

### **3 Response and example**

| Name | Type | Description |
| --- | --- | --- |
| electricMeterId | Int | Electricity meter ID |
| name | String | Electricity meter name |
| number | String | Electricity meter number |
| mac | String | Electricity Meter MAC |
| featureValue | String | Feature value |
| modelNum | String | Product Model |
| hardwareRevision | String | Hardware version number |
| firmwareRevision | String | Firmware version number |
| payMode | Int | Operating modes: 0 - Energy metering mode; 1 - Prepaid mode |
| syncKwhDate | String | Meter reading time |
| online | Int | Online status: 0 - No 1 - Yes |
| rssi | Int | Signal strength |
| onOff | Int | Power-on state |
| totalKwh | String | Total electricity consumption |
| remainderKwh | String | Remaining battery power |
| price | String | Unit price |
| maxPower | String | Maximum power |
| groupName | String | Group Name |
| groupId | Int | Group ID |

```
{
    "syncKwhDate": 1750748374000,
    "modelNum": "SN774-V01",
    "payMode": 0,
    "groupId": 92,
    "electricMeterId": 296,
    "remainderKwh": "10.00",
    "mac": "78:58:44:00:01:41",
    "totalKwh": "9.89",
    "featureValue": "00000000",
    "number": "EM2_410100",
    "groupName": "1",
    "hardwareRevision": "1.3",
    "name": "test meter",
    "online": 0,
    "maxPower": "255",
    "firmwareRevision": "1.1.27.250326",
    "onOff": 1
}
```
