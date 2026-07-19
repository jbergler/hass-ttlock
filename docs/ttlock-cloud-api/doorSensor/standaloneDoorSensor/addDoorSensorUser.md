<!-- Source: https://euopen.ttlock.com/document/doc?urlName=cloud%2FdoorSensor%2FstandaloneDoorSensor%2FaddDoorSensorUserEn.html -->

# Share door sensor

**`https://api.sciener.com/v3/standaloneDoorSensor/addDoorSensorUser`**

Share door sensors with users.

### 1 Request example

`POST, ContentType:application/x-www-form-urlencoded`

```
curl --location -g --request POST 'https://api.sciener.com/v3/standaloneDoorSensor/addDoorSensorUser' \
--data-urlencode 'clientId=4773aa036f7f49c68d876bb4be85c80c' \
--data-urlencode 'accessToken=dfd5489d0cee31f0bdfaf59d0d42d71f' \
--data-urlencode 'doorSensorId=100123' \
--data-urlencode 'account=1@email.com' \
--data-urlencode 'username=username' \
--data-urlencode 'date=1625025703000'
```

### 2 Request parameters

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| clientId | String | Y | client\_id from [Create application](https://euopen.ttlock.com/CreateApplication) |
| accessToken | String | Y | Access token，refer to: [Get access token](https://euopen.ttlock.com/document/doc?urlName=cloud/oauth2/getAccessTokenEn.html) |
| doorSensorId | Int | Y | Door sensor ID |
| account | String | Y | Account |
| username | String | Y | Username |
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
