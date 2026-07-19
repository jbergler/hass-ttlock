<!-- Source: https://euopen.ttlock.com/document/doc?urlName=cloud%2Fuser%2FresetPasswordEn.html -->

# Reset password

**`https://api.sciener.com/v3/user/resetPassword`**

Only password of users registered by cloud API: [User register](https://euopen.ttlock.com/document/doc?urlName=cloud/user/registerEn.html) can be rested，you can't reset password of user accounts registered in Sciener APP。

### 1 Request example

`POST, ContentType:application/x-www-form-urlencoded`

```
curl --location -g --request POST 'https://api.sciener.com/v3/user/resetPassword' \
--header 'Content-Type: application/x-www-form-urlencoded' \
--data-urlencode 'clientId=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx' \
--data-urlencode 'clientSecret=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx' \
--data-urlencode 'username=abcd_c042f4db68f23406c6cecf84a7ebb0fe' \
--data-urlencode 'password=e10adc3949ba59abbe56e057f20f883e' \
--data-urlencode 'date=1625019027000'
```

### 2 Request parameters

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| clientId | String | Y | client\_id from [Create application](https://euopen.ttlock.com/CreateApplication) |
| clientSecret | String | Y | client\_secret from [Create application](https://euopen.ttlock.com/CreateApplication) |
| username | String | Y | The prefixed username return by cloud API: [User register](https://euopen.ttlock.com/document/doc?urlName=cloud/user/registerEn.html) |
| password | String | Y | New password(32 chars, low case, **md5 encrypted**) |
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
