<!-- Source: https://euopen.ttlock.com/document/doc?urlName=cloud%2FdoorSensor%2FlistRecordEn.html -->

# Records of Door Sensor

**`https://api.sciener.com/v3/doorSensor/listRecord`**

Retrieve door sensor opening and closing records

Note: Only operation records within the last six months are retained in the cloud; records older than six months cannot be retrieved.

### 1 Request example

```
curl --location --request GET 'https://api.sciener.com/v3/doorSensor/listRecord?clientId=4773aa036f7f49c68d876bb4be85c80c&accessToken=dfd5489d0cee31f0bdfaf59d0d42d71f&lockId=163377&pageNo=1&pageSize=20&date=1626674054000'
```

### 2 Request parameters

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| clientId | String | Y | Client\_id from [Create application](https://euopen.ttlock.com/CreateApplication) |
| accessToken | String | Y | Access token，refer to: [Get access token](https://euopen.ttlock.com/document/doc?urlName=cloud/oauth2/getAccessTokenEn.html) |
| lockId | Int | Y | Lock ID，Generate by [lock initialize](https://euopen.ttlock.com/document/doc?urlName=cloud/lock/initializeEn.html) |
| pageNo | Int | Y | Page numbers start from 1 |
| pageSize | Int | Y | Number of pages per page: 20 by default, 200 by maximum. |
| date | Long | Y | Current time (timestamp, in milliseconds) |

### 3 Response and example

| Parameter | Type | Description |
| --- | --- | --- |
| list | JSONArray | Record list |
| pageNo | Int | Page numbers start from 1 |
| pageSize | Int | Number of pages per page, maximum 200 |
| pages | Int | Total pages |
| total | Int | Total number of items |

Objects in the list

| Parameter | Type | Description |
| --- | --- | --- |
| recordId | Long | Record ID |
| recordTypeFromLock | Int | For lock record types, please refer to [record Type From Lock](https://euopen.ttlock.com/document/doc?urlName=cloud/lockRecord/recordTypeFromLockEn.html) |
| recordType | Int | For record types, please refer to [record Type From Cloud](https://euopen.ttlock.com/document/doc?urlName=cloud/lockRecord/recordTypeFromCloudEn.html) |
| success | Int | Success status: 0 - No, 1 - Yes |
| lockDate | Long | The time (timestamp, in milliseconds) during the operation is locked. |
| serverDate | Long | Record the time (timestamp, in milliseconds) when the data was uploaded to the server. |

```
{
    "list": [
        {
            "recordId": 1635556821,
            "recordTypeFromLock": 30,
            "recordType": 30,
            "success":1,
            "lockDate": 1616674054000,
            "serverDate": 1616674056000
        }
    ],
    "pageNo":1,
    "pageSize":20,
    "pages":1,
    "total":1
}
```
