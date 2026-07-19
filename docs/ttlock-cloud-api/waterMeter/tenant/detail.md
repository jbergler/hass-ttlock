<!-- Source: https://euopen.ttlock.com/document/doc?urlName=cloud%2FwaterMeter%2Ftenant%2FdetailEn.html -->

# Tenant obtains water meter details

`https://api.sciener.com/v3/waterMeterUser/detail`

This interface doesn't require accessToken

### **1 Request example**

`POST, ContentType:application/x-www-form-urlencoded`

```
curl --location -g --request POST 'https://api.sciener.com/v3/waterMeterUser/detail'\
--data-urlencode 'clientId=4773aa036f7f49c68d876bb4be85c80c' \
--data-urlencode 'mac=11:22:33:44:55:66' \
--data-urlencode 'date=1625025703000'
```

### **2 Request parameters**

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| clientId | String | Y | Assigned client\_id from [created application](https://euopen.ttlock.com/CreateApplicationEn) |
| date | Long | Y | Current time (timestamp, in milliseconds) |
| mac | String | Y | Water Meter MAC |

### 3 Response and example

| Name | Type | Description |
| --- | --- | --- |
| waterMeterId | Int | Water meter ID |
| name | String | Water meter name |
| number | String | Water meter number |
| mac | String | Water meter MAC |
| featureValue | String | Feature value |
| modelNum | String | Product Model |
| hardwareRevision | String | Hardware version number |
| firmwareRevision | String | Firmware version number |
| payMode | Int | Operating modes: 0 - Energy metering mode; 1 - Prepaid mode |
| syncM3Date | String | Meter reading time |
| online | Int | Online status: 0 - No 1 - Yes |
| rssi | Int | Signal strength |
| onOff | Int | Water flow status |
| totalM3 | String | Total water consumption |
| remainderM3 | String | Remaining water volume |
| price | String | Unit price |
| executeToken | String | If the token used during the recharge is not empty, it indicates that there are pending tasks. |

```
{
    "syncM3Date": 1750748374000,
    "modelNum": "SN774-V01",
    "payMode": 0,
    "groupId": 92,
    "waterMeterId": 296,
    "remainderM3": "10.00",
    "mac": "78:58:44:00:01:41",
    "totalM3": "9.89",
    "featureValue": "00000000",
    "number": "EM2_410100",
    "groupName": "1",
    "hardwareRevision": "1.3",
    "name": "water meter",
    "online": 0,
    "maxPower": "255",
    "firmwareRevision": "1.1.27.250326",
    "onOff": 1
}
```
