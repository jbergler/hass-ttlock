<!-- Source: https://euopen.ttlock.com/document/doc?urlName=cloud%2FwirelessKeypad%2FupgradeSuccessEn.html -->

# Check if upgrade was successful

**`https://api.sciener.com/v3/wirelessKeypad/upgradeSuccess`**

### 1 Request example

`POST, ContentType:application/x-www-form-urlencoded`

```
curl --location -g --request POST 'https://api.sciener.com/v3/wirelessKeypad/upgradeSuccess' \
--data-urlencode 'clientId=4773aa036f7f49c68d876bb4be85c80c' \
--data-urlencode 'accessToken=dfd5489d0cee31f0bdfaf59d0d42d71f' \
--data-urlencode 'wirelessKeypadId=3001' \
--data-urlencode 'slotNumber=1' \
--data-urlencode 'featureValue=7' \
--data-urlencode 'date=1625025703000'
```

### 2 Request parameters

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| clientId | String | Y | client\_id from [Create application](https://euopen.ttlock.com/CreateApplication) |
| accessToken | String | Y | access Toke，refer to [get access token](https://euopen.ttlock.com/document/doc?urlName=cloud/oauth2/getAccessToken.html) |
| wirelessKeypadId | Int | Y | wireless keypad ID |
| slotNumber | Int | Y | slot number |
| featureValue | String | N | feature value |
| date | Long | Y | current time (timestamp in millisecond) |

### 3 Response and example

| Parameters | Type | Description |
| --- | --- | --- |
| errcode | Int | error code |
| errmsg | String | error message |
| errmsg | String | error message |

```
{
    "errcode": 0, 
    "errmsg": "none error message",
    "description":"表示成功或是"
}
```
