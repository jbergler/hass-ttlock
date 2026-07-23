<!-- Source: https://euopen.ttlock.com/document/doc?urlName=cloud%2FeletricMeter%2Ftenant%2FlistRechargeEn.html -->

# Tenant query operation records

`https://api.sciener.com/v3/electricMeterUser/listRecharge`

This interface doesn't require accessToken

### **1 Request example**

`POST, ContentType:application/x-www-form-urlencoded`

```
curl --location -g --request POST 'https://api.sciener.com/v3/electricMeterUser/listRecharge' \
--data-urlencode 'clientId=4773aa036f7f49c68d876bb4be85c80c' \
--data-urlencode 'electricMeterId=3001' \
--data-urlencode 'pageNo=1' \
--data-urlencode 'pageSize=20' \
--data-urlencode 'date=1625025703000'
```

### **2 Request parameters**

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| clientId | String | Y | Assigned client\_id from [created application](https://euopen.ttlock.com/CreateApplicationEn) |
| date | Long | Y | Current time (timestamp, in milliseconds) |
| electricMeterId | String | Y | Electricity meter ID |
| recordType | String | N | Operation types: 1: Recharge, 2: Power off, 3: Power on, 4: Meter reading, 5: Unit price modification, 6: Reset |
| searchStr | String | N | Search results |
| pageNo | Int | Y | Page numbers start from 1 |
| pageSize | Int | Y | Number of pages per page, maximum 1000 |

### **3 Response and example**

| Name | Type | Description |
| --- | --- | --- |
| list | JSONObject | List |

Objects in the list

| Name | Type | Description |
| --- | --- | --- |
| id | Int | Record ID |
| recordType | String | Operation types: -1: Date interval; 1: Clear remaining battery power; 2: Switch payment type; 4: Power on/off; 5: Meter reading; 6: Update unit price; 7: Administrator top-up; 8: Tenant top-up. |
| title | String | Title |
| content | String | Content |
| date | String | Operation time |
| headUrl | String | Operator's avatar |

```
{
    "list": [
        {
            "date": "2025-06-24",
            "recordType": -1
        },
        {
            "date": "14:59:34",
            "recordType": 5,
            "headUrl": "http://qiniuroommaster.sciener.cn/avator/Sciener99964251735882500842.jpg?sign=d4895923cadd4b07b5d247833b31714c&t=685cf58c",
            "id": 13577,
            "title": "135 black",
            "content": "抄表，总电量9.89kWh。"
        }
    ]
}
```
