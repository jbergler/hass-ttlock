<!-- Source: https://euopen.ttlock.com/document/doc?urlName=cloud%2Fuser%2FlistEn.html -->

# Get user list

**`https://api.sciener.com/v3/user/list`**

List the users registered by cloud API: [Register user](https://euopen.ttlock.com/document/doc?urlName=cloud/user/registerEn.html)，this API will not return users registered in Sciener APP.

### 1 Request example

```
curl --location -g --request GET 'https://api.sciener.com/v3/user/list?clientId=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx&clientSecret=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx&date=1625025703000&pageNo=1&pageSize=20'
```

### 2 Request parameters

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| clientId | String | Y | client\_id from [Create application](https://euopen.ttlock.com/CreateApplication) |
| clientSecret | String | Y | client\_secret from [Create application](https://euopen.ttlock.com/CreateApplication) |
| startDate | Long | N | query by register time，start time (timestamp in millisecond) |
| endDate | Long | N | query by register time，end time (timestamp in millisecond) |
| pageNo | Int | Y | Page no, start from 1 |
| pageSize | Int | Y | Items per page, max 200 |
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
| username | String | The prefixed username return by cloud API: [User register](https://euopen.ttlock.com/document/doc?urlName=cloud/user/registerEn.html) |
| regtime | Long | Register time(timestamp in milliseconds) |

```
{
    "list": [
        {
            "username": "abcd_c042f4db68f23406c6cecf84a7ebb0fe",
            "regtime": 1625019027000,
        }
    ],
    "pageNo":1,
    "pageSize":20,
    "pages":1,
    "total":1
}
```
