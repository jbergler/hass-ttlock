<!-- Source: https://euopen.ttlock.com/document/doc?urlName=cloud%2FwaterMeter%2Fadmin%2FlistOperationRecordsEn.html -->

# Operation record list

`https://api.sciener.com/v3/waterMeter/listOperationRecords`

### **1 Request example**

`POST, ContentType:application/x-www-form-urlencoded`

```
curl --location -g --request POST 'https://api.sciener.com/v3/waterMeter/listOperationRecords' \
--data-urlencode 'clientId=4773aa036f7f49c68d876bb4be85c80c' \
--data-urlencode 'accessToken=dfd5489d0cee31f0bdfaf59d0d42d71f' \
--data-urlencode 'waterMeterId=3001' \  
--data-urlencode 'pageNo=1' \
--data-urlencode 'pageSize=20' \
--data-urlencode 'date=1625025703000'
```

### **2 Request parameters**

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| clientId | String | Y | Assigned client\_id from [created application](https://euopen.ttlock.com/CreateApplicationEn) |
| accessToken | String | Y | Access token， obtain by calling [get access token](https://euopen.ttlock.com/document/doc?urlName=cloud/oauth2/getAccessTokenEn.html) API |
| date | Long | Y | Current time (timestamp, in milliseconds) |
| waterMeterId | String | Y | Water Meter ID |
| recordType | String | N | Operation types: 1: Recharge, 2: Water outage, 3: Water supply restored, 4: Meter reading, 5: Price modification, 6: Reset. |
| searchStr | String | N | Search terms |
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
| recordType | String | Operation type: -1: Date interval; 1: Clear remaining water volume; 2: Switch payment type; 4: Water supply on/off; 5: Meter reading; 6: Update unit price; 7: recharge; 8: set water amount; 9: recharging |
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
            "content": "Meter reading: Total water consumption 9.89 m³"
        }
    ]
}
```
