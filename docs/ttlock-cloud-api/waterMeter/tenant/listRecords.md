<!-- Source: https://euopen.ttlock.com/document/doc?urlName=cloud%2FwaterMeter%2Ftenant%2FlistRecordsEn.html -->

# Tenant can check the list of water usage records

`https://api.sciener.com/v3/waterMeterUser/listRecords`

This interface doesn't require accessToken

### 1 Request example

`POST, ContentType:application/x-www-form-urlencoded`

```
curl --location -g --request POST 'https://api.sciener.com/v3/waterMeterUser/listRecords' \
--data-urlencode 'clientId=4773aa036f7f49c68d876bb4be85c80c' \
--data-urlencode 'waterMeterId=3001' \
--data-urlencode 'date=1625025703000'
```

### **2 Request parameters**

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| clientId | String | Y | Assigned client\_id from [created application](https://euopen.ttlock.com/CreateApplicationEn) |
| dateStr | String | N | Search criteria: 2024-09-06, 2024-09, or 2024 |
| date | Long | Y | Current time (timestamp, in milliseconds) |
| waterMeterId | String | Y | Water Meter ID |
| dateType | Int | Y | Date type, 0: day, 1: month, 2: year |

### 3 Response and example

| Name | Type | Description |
| --- | --- | --- |
| list | JSONObject | List |

Objects in the list

| Name | Type | Description |
| --- | --- | --- |
| dateType | Int | Date type: 0: day, 1: month, 2: year |
| usedKwh | String | Water consumption |
| createDate | String | Operation time |

```
{
    "list": [
        {
            "dateType": 2,
            "usedM3": "0.00t",
            "createDate": "2025-06"
        }
    ]
}
```
