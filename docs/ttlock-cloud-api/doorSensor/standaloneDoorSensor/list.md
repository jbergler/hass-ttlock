<!-- Source: https://euopen.ttlock.com/document/doc?urlName=cloud%2FdoorSensor%2FstandaloneDoorSensor%2FlistEn.html -->

# Query the list of DS3

**`https://api.sciener.com/v3/standaloneDoorSensor/list`**

### 1 Request example

`POST, ContentType:application/x-www-form-urlencoded`

```
curl --location -g --request POST 'https://api.sciener.com/v3/standaloneDoorSensor/list' \
--data-urlencode 'clientId=4773aa036f7f49c68d876bb4be85c80c' \
--data-urlencode 'accessToken=dfd5489d0cee31f0bdfaf59d0d42d71f' \
--data-urlencode 'pageNo=1' \
--data-urlencode 'pageSize=20' \
--data-urlencode 'date=1625025703000'
```

### 2 Request parameters

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| clientId | String | Y | client\_id from [Create application](https://euopen.ttlock.com/CreateApplication) |
| accessToken | String | Y | Access token，refer to: [Get access token](https://euopen.ttlock.com/document/doc?urlName=cloud/oauth2/getAccessTokenEn.html) |
| pageNo | Int | Y | Page numbers start from 1 |
| pageSize | Int | Y | Number of pages per page, maximum 200 |
| searchStr | String | N | Search door sensor name |
| date | Long | Y | Current time (timestamp, in milliseconds) |

### 3 Response and example

| Parameter | Type | Description |
| --- | --- | --- |
| list | JSONObject | list |

objects in the list

| Parameter | Type | Description |
| --- | --- | --- |
| mac | String | MAC |
| featureValue | String | Feature value |
| statusDelayFlag | Int | 1: Device status has not been updated for a long time; 0: No |
| electricQuantity | Int | Battery level |
| doorSensorState | Int | Door sensor status: 0 - Closed, 1 - Open, 2 - Unknown |
| wifiName | String | WIFI name |
| doorSensorNumber | String | Door sensor number |
| doorSensorId | Int | Door sensor ID |
| name | String | Name |
| recordUpdateDate | Long | Record update time |
| wifiMac | String | WIFI MAC address |
| userType | String | User type: ADMIN: Administrator, USER: User being shared with. |
| firmwareRevision | String | Firmware version |

```
{
    "list": [
        {
            "mac": "8C:1F:64:12:5D:2E",
            "featureValue": "00000001",
            "statusDelayFlag": 0,
            "electricQuantity": 100,
            "doorSensorState": 1,
            "wifiName": "@Ruijie-sAFEF",
            "doorSensorNumber": "DS3_2e5d12",
            "doorSensorId": 67,
            "name": "door sensor",
            "recordUpdateDate": 1763459321000,
            "wifiMac": "2F:5D:12:64:1F:8C",
            "userType": "ADMIN",
            "firmwareRevision": "1.0.06.251112"
        }
    ]
}
```

#### 3.1 Feature value description

Similar to the lock's characteristic value, the individual door sensor's characteristic value is also a hexadecimal string, indicating what functions this individual door lock supports.

The function represented by each byte bit (counting from 0) after high-low bit conversion is as follows:

| Bit | explanation |
| --- | --- |
| 0 | support 2.4G WiFi |
