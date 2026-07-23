<!-- Source: https://euopen.ttlock.com/document/doc?urlName=cloud%2Fuser%2FregisterEn.html -->

# User register

**`https://api.sciener.com/v3/user/register`**

**Alert：If you have already registered an account in Sciener APP, there is no need to call this api to register a new account，just [Get Access Token](https://euopen.ttlock.com/document/doc?urlName=cloud/oauth2/getAccessTokenEn.html) with the account.**

If you already have or plan to maintain your own user info in your database，and do not want to let us to know your user info，you can create an user account via this API and bind it with the user in your system.

For example：

（1）Your client registered an email account ([alexa@google.com](mailto:alexa@google.com)) in your system；

（2）You request this API to register an user in cloud server，`username` parameter can be a random string, for example：`c042f4db68f23406c6cecf84a7ebb0fe`，cloud API will return a prefixed username：`abcd_c042f4db68f23406c6cecf84a7ebb0fe`，the prefix `abcd_`is randomly generated when [Create application](https://euopen.ttlock.com/CreateApplication)，it is used for user namespace of different applications，there is no other use of it, it can't be customized；

（3）You maintain a relation between your user account [alexa@google.com](mailto:alexa@google.com) and open cloud's user account `abcd_c042f4db68f23406c6cecf84a7ebb0fe`；

（4）When the user [alexa@google.com](mailto:alexa@google.com) have to access cloud API，[Get Access Token](https://euopen.ttlock.com/document/doc?urlName=cloud/oauth2/getAccessTokenEn.html) with username `abcd_c042f4db68f23406c6cecf84a7ebb0fe`.

### 1 Request example

`POST, ContentType:application/x-www-form-urlencoded`

```
curl --location -g --request POST 'https://api.sciener.com/v3/user/register' \
--header 'Content-Type: application/x-www-form-urlencoded' \
--data-urlencode 'clientId=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx' \
--data-urlencode 'clientSecret=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx' \
--data-urlencode 'username=c042f4db68f23406c6cecf84a7ebb0fe' \
--data-urlencode 'password=e10adc3949ba59abbe56e057f20f883e' \
--data-urlencode 'date=1625019027000'
```

### 2 Request parameters

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| clientId | String | Y | client\_id from [Create application](https://euopen.ttlock.com/CreateApplication) |
| clientSecret | String | Y | client\_secret from [Create application](https://euopen.ttlock.com/CreateApplication) |
| username | String | Y | username，only numbers and English letters allowed，this api will return a prefixed username, please [GET Access Token](https://euopen.ttlock.com/document/doc?urlName=cloud/oauth2/getAccessTokenEn.html) with the prefixed username |
| password | String | Y | Password(32 chars, low case, **md5 encrypted**) |
| date | Long | Y | Current time (timestamp in millisecond) |

### 3 Response and example

| Parameter | Type | Description |
| --- | --- | --- |
| username | String | Prefixed username, please [GET Access Token](https://euopen.ttlock.com/document/doc?urlName=cloud/oauth2/getAccessTokenEn.html) with this prefixed username |

```
{
    "username": "abcd_c042f4db68f23406c6cecf84a7ebb0fe"
}
```
