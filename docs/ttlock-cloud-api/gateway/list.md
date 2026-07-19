<!-- Source: https://euopen.ttlock.com/document/doc?urlName=cloud%2Fgateway%2FlistEn.html -->

# Get the gateway list of an account

**`https://api.sciener.com/v3/gateway/list`**

List the gateways belong to a user account.

### 1 Request example

```
curl --location --request GET 'https://api.sciener.com/v3/gateway/list?clientId=4773aa036f7f49c68d876bb4be85c80c&accessToken=dfd5489d0cee31f0bdfaf59d0d42d71f&pageNo=1&pageSize=20&date=1626674054000'
```

### 2 Request parameters

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| clientId | String | Y | client\_id from [Create application](https://euopen.ttlock.com/CreateApplication) |
| accessToken | String | Y | Access token，refer to: [Get access token](https://euopen.ttlock.com/document/doc?urlName=cloud/oauth2/getAccessTokenEn.html) |
| pageNo | Int | Y | Page no, start from 1 |
| pageSize | Int | Y | Items per page, max 200 |
| orderBy | Int | Y | Sort by: 0-by name, 1-reverse order by time, 2-reverse order by name |
| date | Long | Y | Current time (timestamp in millisecond) |

### 3 Response and example

| Parameter | Type | Description |
| --- | --- | --- |
| list | JSONArray | list of records |
| pageNo | Int | Page no, start from 1 |
| pageSize | Int | Items per page, max 200 |
| pages | Int | Total number of pages |
| total | Int | Total number of records |

The objects in the list

| Parameter | Type | Description |
| --- | --- | --- |
| gatewayId | Int | Gateway ID |
| gatewayMac | String | Gateway MAC |
| gatewayName | String | Gateway name |
| gatewayVersion | Int | The version of gateway, 1-G1，2-G2，3-G3(wired gateway)，4-G4（4G gateway） |
| networkName | String | The network name gateway connected to. |
| networkMac | String | Mac address of the network to which the gateway is connected |
| lockNum | Int | The number of locks connected to the gateway. |
| isOnline | Int | Is it online: 0-No, 1-Yes. The gateway will maintain a TCP connection with the gateway server, the cloud server consider it online if the connection is present, plug off the gateway may not break the connection immediately, it may take several minutes for the gateway server to break the connection after heartbeat timeout. |

```
{
    "list": [
        {
            "gatewayId": 78979,
            "gatewayMac": "C5:40:E0:9C:8C:C1",
            "gatewayVersion":1,
            "networkName": "1-101-WIFI",
            "lockNum": 2,
            "isOnline": 1   
        }
    ],
    "pageNo":1,
    "pageSize":20,
    "pages":1,
    "total":1
}
```
