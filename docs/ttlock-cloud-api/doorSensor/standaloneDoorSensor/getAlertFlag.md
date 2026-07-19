<!-- Source: https://euopen.ttlock.com/document/doc?urlName=cloud%2FdoorSensor%2FstandaloneDoorSensor%2FgetAlertFlagEn.html -->

# Get DS3 alert

**`https://api.sciener.com/v3/standaloneDoorSensor/getAlertFlag`**

The notification received in the cloud will be sent to the developer via a callback.

### 1 Request example

`POST, ContentType:application/x-www-form-urlencoded`

```
curl --location -g --request POST 'https://api.sciener.com/v3/standaloneDoorSensor/getAlertFlag' \
--data-urlencode 'clientId=4773aa036f7f49c68d876bb4be85c80c' \
--data-urlencode 'accessToken=dfd5489d0cee31f0bdfaf59d0d42d71f' \
--data-urlencode 'doorSensorId=100123' \
--data-urlencode 'date=1625025703000'
```

### 2 Request parameters

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| clientId | String | Y | client\_id from [Create application](https://euopen.ttlock.com/CreateApplication) |
| accessToken | String | Y | Access token，refer to: [Get access token](https://euopen.ttlock.com/document/doc?urlName=cloud/oauth2/getAccessTokenEn.html) |
| doorSensorId | Int | Y | DS3 ID |
| date | Long | Y | Current time (timestamp, in milliseconds) |

### 3 Response and example

| Parameter | Type | Description |
| --- | --- | --- |
| notCloseAlertFlag | Int | Door not closed reminder indicator: Value range 1 - Open, 2 - Closed |
| notCloseAlertSecondNum | Int | Door not closed reminder time, unit: seconds, range: 1~900 seconds. |
| longTimeNotOpenAlertFlag | Int | Long-term inactivity reminder indicator: Value range 1 - On, 2 - Off |
| longTimeNotOpenDayNum | Int | Long-term door inactivity reminder time, unit: days, range: 1-15 days. |

```
{
    "notCloseAlertFlag": 1, 
    "notCloseAlertSecondNum": 100,
    "longTimeNotOpenAlertFlag": 1,
    "longTimeNotOpenDayNum": 10
}
```
