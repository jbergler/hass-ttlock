<!-- Source: https://euopen.ttlock.com/document/doc?urlName=cloud%2Fgroup%2FlistEn.html -->

# Get the group list of an account

**`https://api.sciener.com/v3/group/list`**

### 1 Request example

```
curl --location -g --request GET 'https://api.sciener.com/v3/group/list?clientId=fd2ff35ee3d8424c8665c07b7b9a7f45&accessToken=780f7671b6e6be3010d9787f19207aca&date=1625025703000'
```

### 2 Request parameters

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| clientId | String | Y | client\_id from [Create application](https://euopen.ttlock.com/CreateApplication) |
| accessToken | String | Y | Access token，refer to: [Get access token](https://euopen.ttlock.com/document/doc?urlName=cloud/oauth2/getAccessTokenEn.html) |
| orderBy | Int | Y | Sort by: 0-by name, 1-reverse order by time, 2-reverse order by name |
| date | Long | Y | Current time (timestamp in millisecond) |

### 3 Response and example

| Parameter | Type | Description |
| --- | --- | --- |
| list | JSONArray | list of records |

The objects in the list

| Parameter | Type | Description |
| --- | --- | --- |
| groupId | Int | Group ID |
| name | String | Group name |

```
{
    "list": [
        {
            "groupId": 1342,
            "name":"The 4th floor"
        }
    ]
}
```
