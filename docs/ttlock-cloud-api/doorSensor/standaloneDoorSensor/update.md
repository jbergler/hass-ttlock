<!-- Source: https://euopen.ttlock.com/document/doc?urlName=cloud%2FdoorSensor%2FstandaloneDoorSensor%2FupdateEn.html -->

# Change the name of the door sensor

**`https://api.sciener.com/v3/standaloneDoorSensor/update`**

### 1 Request example

`POST, ContentType:application/x-www-form-urlencoded`

```
curl --location -g --request POST 'https://api.sciener.com/v3/standaloneDoorSensor/update' \
--data-urlencode 'clientId=4773aa036f7f49c68d876bb4be85c80c' \
--data-urlencode 'accessToken=dfd5489d0cee31f0bdfaf59d0d42d71f' \
--data-urlencode 'doorSensorId=100123' \
--data-urlencode 'name=name' \
--data-urlencode 'date=1625025703000'
```

### 2 Request parameters

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| clientId | String | Y | client\_id from [Create application](https://euopen.ttlock.com/CreateApplication) |
| accessToken | String | Y | Access token，refer to: [Get access token](https://euopen.ttlock.com/document/doc?urlName=cloud/oauth2/getAccessTokenEn.html) |
| doorSensorId | Int | Y | Standalone door sensor ID |
| name | String | Y | Name |
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
