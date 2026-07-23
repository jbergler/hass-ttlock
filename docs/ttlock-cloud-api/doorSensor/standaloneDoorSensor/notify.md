<!-- Source: https://euopen.ttlock.com/document/doc?urlName=cloud%2FdoorSensor%2FstandaloneDoorSensor%2FnotifyEn.html -->

# Callback notifications of DS3

This interface is implemented by the developer. After the application is approved, the developer can set the implemented callback interface address in the application details page of the management center.

When an independent door sensor records a data entry or alarm, the record is automatically read and uploaded to the cloud. The cloud then calls the interface provided by the developer to achieve near real-time notification of the record.

Note: It is necessary to ensure that the administrator account of the independent door sensor has obtained an access token using the current application's clientId for the current application to receive record push notifications.

### 1 Request example

`POST, ContentType:application/x-www-form-urlencoded`

```
curl --location -g --request POST 'https://example.com/lockRecord/callback' \
--data-urlencode 'notifyType=7' \
--data-urlencode 'doorSensorId=100123' \
--data-urlencode 'mac=AA:BB:CC:DD:EE:FF' \
--data-urlencode 'recordType=1' \
```

Please note: When testing, please replace <https://example.com/lockRecord/callback> with the implemented callback address.

### 2 Request parameters

| Name | Type | Description |
| --- | --- | --- |
| notifyType | Int | 7 indicates a callback notification for the independent door magnetic sensor's open/closed status. |
| doorSensorId | Int | Door sensor ID |
| mac | String | Door sensor MAC address |
| name | String | Door sensor name |
| recordType | Int | 0: Closed. 1: Open. 2: Door not closed alarm. 3: Door not opened for an extended period alarm. |

### 3 Response and example

Return the string "success" in the response body.
