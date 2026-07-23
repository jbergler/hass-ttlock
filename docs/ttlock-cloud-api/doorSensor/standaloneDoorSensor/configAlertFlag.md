<!-- Source: https://euopen.ttlock.com/document/doc?urlName=cloud%2FdoorSensor%2FstandaloneDoorSensor%2FconfigAlertFlagEn.html -->

# Configure alert

**`https://api.sciener.com/v3/standaloneDoorSensor/configAlertFlag`**

After saving the reminder configuration by calling this interface, you need to open and close the door sensor again for the configuration to take effect.

### 1 Request example

`POST, ContentType:application/x-www-form-urlencoded`

```
curl --location -g --request POST 'https://api.sciener.com/v3/standaloneDoorSensor/configAlertFlag' \
--data-urlencode 'clientId=4773aa036f7f49c68d876bb4be85c80c' \
--data-urlencode 'accessToken=dfd5489d0cee31f0bdfaf59d0d42d71f' \
--data-urlencode 'doorSensorId=100123' \
--data-urlencode 'notCloseAlertFlag=1' \
--data-urlencode 'notCloseAlertSecondNum=100' \
--data-urlencode 'longTimeNotOpenAlertFlag=1' \
--data-urlencode 'longTimeNotOpenDayNum=10' \
--data-urlencode 'date=1625025703000'
```

### 2 Request parameters

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| clientId | String | Y | Client\_id from [Create application](https://euopen.ttlock.com/CreateApplication) |
| accessToken | String | Y | Access token，refer to: [Get access token](https://euopen.ttlock.com/document/doc?urlName=cloud/oauth2/getAccessTokenEn.html) |
| doorSensorId | Int | Y | Door sensor ID |
| notCloseAlertFlag | Int | Y | Door not closed reminder indicator: Value range 1 - Open, 2 - Closed |
| notCloseAlertSecondNum | Int | N | Door not closed reminder time, unit: seconds, range: 1~900 seconds. |
| longTimeNotOpenAlertFlag | Int | Y | Long-term inactivity reminder indicator: Value range 1 - On, 2 - Off |
| longTimeNotOpenDayNum | Int | N | Long-term door inactivity reminder time, unit: days, range: 1-15 days. |
| date | Long | Y | Current time (timestamp, in milliseconds) |

### 3 Response and example

| Parameter | Type | Description |
| --- | --- | --- |
| errcode | Int | Error code |
| errmsg | String | Error message |
| errmsg | String | Error information |

```
{
    "errcode": 0, 
    "errmsg": "none error message",
    "description":"表示成功或是"
}
```
