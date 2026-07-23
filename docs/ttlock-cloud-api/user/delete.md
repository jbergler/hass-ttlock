<!-- Source: https://euopen.ttlock.com/document/doc?urlName=cloud%2Fuser%2FdeleteEn.html -->

# Delete user

**`https://api.sciener.com/v3/user/delete`**

Only user registered by cloud API: [User register](https://euopen.ttlock.com/document/doc?urlName=cloud/user/registerEn.html) can be deleted，you can't delete user account registered in Sciener APP。

If a user is deleted, it's access token and ekeys will also be deleted.

If the user still owns lock(s), it can't be deleted, it's locks should be deleted or transfered to others before it can be deleted .

### 1 Request example

`POST, ContentType:application/x-www-form-urlencoded`

```
curl --location -g --request POST 'https://api.sciener.com/v3/user/delete' \
--header 'Content-Type: application/x-www-form-urlencoded' \
--data-urlencode 'clientId=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx' \
--data-urlencode 'clientSecret=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx' \
--data-urlencode 'username=abcd_c042f4db68f23406c6cecf84a7ebb0fe' \
--data-urlencode 'date=1625019027000'
```

### 2 Request parameters

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| clientId | String | Y | client\_id from [Create application](https://euopen.ttlock.com/CreateApplication) |
| clientSecret | String | Y | client\_secret from [Create application](https://euopen.ttlock.com/CreateApplication) |
| username | String | Y | The prefixed username return by cloud API: [User register](https://euopen.ttlock.com/document/doc?urlName=cloud/user/registerEn.html) |
| date | Long | Y | Current time (timestamp in millisecond) |

### 3 Response and example

| Parameter | Type | Description |
| --- | --- | --- |
| errcode | Int | Error code |
| errmsg | String | Error message |

```
{
    "errcode": 0, 
    "errmsg": "none error message"
}
```
