<!-- Source: https://euopen.ttlock.com/document/doc?urlName=cloud%2Fgateway%2FlistLockEn.html -->

# Get the lock list of a gateway

**`https://api.sciener.com/v3/gateway/listLock`**

The gateway automatically search locks nearby it, and notify the gateway server about the locks it found, the gateway server will create a many-to-many relationship between the gateways and locks if the lock and gateway belong to the same administrator account, the relationship will be cached for 30 minutes, that is, if the gateway have notified the gateway server about a lock in 30 minutes, this API will return the lock.

This API will return all the locks related to a gateway.

### 1 Request example

```
curl --location --request GET 'https://api.sciener.com/v3/gateway/listLock?clientId=4773aa036f7f49c68d876bb4be85c80c&accessToken=dfd5489d0cee31f0bdfaf59d0d42d71f&gatewayId=78979&date=1626674054000'
```

### 2 Request parameters

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| clientId | String | Y | client\_id from [Create application](https://euopen.ttlock.com/CreateApplication) |
| accessToken | String | Y | Access token，refer to: [Get access token](https://euopen.ttlock.com/document/doc?urlName=cloud/oauth2/getAccessTokenEn.html) |
| gatewayId | Int | Y | Gateway ID |
| date | Long | Y | Current time (timestamp in millisecond) |

### 3 Response and example

| Parameter | Type | Description |
| --- | --- | --- |
| list | JSONArray | list of records |

The objects in the list：

| Parameter | Type | Description |
| --- | --- | --- |
| lockId | Int | Lock ID |
| lockMac | String | Lock MAC |
| lockName | String | Lock name |
| lockAlias | String | Lock alias |
| rssi | Int | The signal intensity between gateway and lock. reference: >-75 is strong, -85<i<-75 is medium, <-85 is weak |
| updateDate | Long | The time when the signal intensity updated (timestamp in millisecond) |

```
{
    "list": [
        {
            "lockId": 532323,
            "lockName":"YS1003_c18c9c",
            "lockAlias":"Front door lock",
            "lockMac": "C5:40:E0:9C:8C:C1",
            "rssi":-65,
            "updateDate": 1626674053000
        }
    ]
}
```
