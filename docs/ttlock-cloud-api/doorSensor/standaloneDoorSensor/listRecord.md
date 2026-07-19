<!-- Source: https://euopen.ttlock.com/document/doc?urlName=cloud%2FdoorSensor%2FstandaloneDoorSensor%2FlistRecordEn.html -->

# Query DS3 operation records

**`https://api.sciener.com/v3/standaloneDoorSensor/listRecord`**

### 1 Request example

`POST, ContentType:application/x-www-form-urlencoded`

```
curl --location -g --request POST 'https://api.sciener.com/v3/standaloneDoorSensor/listRecord' \
--data-urlencode 'clientId=4773aa036f7f49c68d876bb4be85c80c' \
--data-urlencode 'accessToken=dfd5489d0cee31f0bdfaf59d0d42d71f' \
--data-urlencode 'doorSensorId=doorSensorId' \
--data-urlencode 'pageNo=1' \
--data-urlencode 'pageSize=20' \
--data-urlencode 'date=1625025703000'
```

### 2 Request parameters

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| clientId | String | Y | client\_id from [Create application](https://euopen.ttlock.com/CreateApplication) |
| accessToken | String | Y | Access token，refer to: [Get access token](https://euopen.ttlock.com/document/doc?urlName=cloud/oauth2/getAccessTokenEn.html) |
| doorSensorId | Int | Y | Door sensor ID |
| pageNo | Int | Y | Page numbers start from 1 |
| pageSize | Int | Y | Number of pages per page, maximum 200 |
| date | Long | Y | Current time (timestamp, in milliseconds) |

### 3 Response and example

| Parameter | Type | Description |
| --- | --- | --- |
| list | JSONObject | list |

objects in the list

| Parameter | Type | Description |
| --- | --- | --- |
| date | String | Record time (timestamp, in milliseconds). |
| recordType | Int | 0: Closed. 1: Open. 2: Door not closed alarm. 3: Door not opened for an extended period alarm. |

```
{
    "list": [
        {
            "date": 1763532257000,
            "recordType": 1
        }
    ]
}
```
