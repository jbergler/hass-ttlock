<!-- Source: https://euopen.ttlock.com/document/doc?urlName=cloud%2Fgateway%2FlistDeviceEn.html -->

# Get device list of a gateway

**`https://api.sciener.com/v3/gateway/listDevice`**

The gateway automatically searches for nearby devices belonging to the same administrator and reports them to the cloud to establish connections. This interface returns all devices connected to the gateway.

Connection records are cached for 30 minutes (this may be adjusted later). That is, as long as the gateway finds a device and reports it to the cloud within 30 minutes (the corresponding RSSI updateDate time is within 30 minutes), the cloud determines that the gateway and the device are connected. Therefore, this interface will not return the corresponding device information 30 minutes after the gateway is powered off.

Device include locks, electricity meters, and water meters.

### 1 Request example

```
curl --location --request GET 'https://api.sciener.com/v3/gateway/listDevice?clientId=4773aa036f7f49c68d876bb4be85c80c&accessToken=dfd5489d0cee31f0bdfaf59d0d42d71f&gatewayId=78979&date=1626674054000'
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
| list | JSONArray | record list |

The objects in the list：

| Parameter | Type | Description |
| --- | --- | --- |
| deviceType | Int | Equipment type 0: Lock. 1: Electricity meter. 2: Water meter. |
| deviceId | Int | Device ID |
| deviceMac | String | Device MAC address |
| deviceName | String | Device Bluetooth name |
| deviceAlias | String | Equipment Alias |
| rssi | Int | For signal strength between the gateway and the device, the reference standard is: greater than -75 is strong, greater than -85 and less than -75 is medium, and less than -85 is weak. |
| updateDate | Long | RSSI signal strength update time |

```
{
    "list": [
        {
            "deviceType": 0,
            "deviceId": 532323,
            "deviceName":"YS1003_c18c9c",
            "deviceAlias":"entrance lock",
            "deviceMac": "C5:40:E0:9C:8C:C1",
            "rssi":-65,
            "updateDate": 1626674053000
        }
    ]
}
```
