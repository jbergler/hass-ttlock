<!-- Source: https://euopen.ttlock.com/document/doc?urlName=cloud%2FeletricMeter%2Ftenant%2FlistRecordsEn.html -->

# Tenant query electricity usage records list

`https://api.sciener.com/v3/electricMeterUser/listRecords`

This interface doesn't require accessToken

### 1 Request example

`POST, ContentType:application/x-www-form-urlencoded`

```
curl --location -g --request POST 'https://api.sciener.com/v3/electricMeterUser/listRecords' \
--data-urlencode 'clientId=4773aa036f7f49c68d876bb4be85c80c' \
--data-urlencode 'electricMeterId=3001' \
--data-urlencode 'date=1625025703000'
```

### **2 Request parameters**

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| clientId | String | Y | Assigned client\_id from [created application](https://euopen.ttlock.com/CreateApplicationEn) |
| date | Long | Y | Current time (timestamp, in milliseconds) |
| electricMeterId | String | Y | Electricity meter ID |
| dateType | Int | Y | Date type, 0: day, 1: month, 2: year |
| dateStr | String | Y | Search criteria: 2024-09-06, 2024-09, or 2024 |

### **3 Response and example**

| Name | Type | Description |
| --- | --- | --- |
| list | JSONObject | List |

Objects in the li's't

| Name | Type | Description |
| --- | --- | --- |
| dateType | Int | Date type: 0: day, 1: month, 2: year |
| usedKwh | String | Electricity consumption |
| createDate | String | Operation time |

```
{
    "list": [
        {
            "dateType": 2,
            "usedKwh": "0.00kWh",
            "createDate": "2025-06"
        }
    ]
}
```
