<!-- Source: https://euopen.ttlock.com/document/doc?urlName=cloud%2Foauth2%2FgetAccessTokenEn.html -->

# Get access token

**`https://api.sciener.com/oauth2/token`**

Cloud APIs grant access by [OAuth 2.0](https://oauth.net/2/) 's Resource Owner Password grant type，you have to request with username and password，all the cloud apis should be requested with access token.

**Note: The username and password here are the account of the TTLock app. You need to download the TTLock app and register. please do not use your open platform's developer account.**

Note：access token returned will expire in seconds of `expires_in`（Default validity of 90 days），request with expired access token will get 10004 error code，you should get a new token with this API, or [Refresh the access token](https://euopen.ttlock.com/document/doc?urlName=cloud/oauth2/refreshAccessTokenEn.html).

### 1 Request example

`POST, ContentType:application/x-www-form-urlencoded`

```
curl --location -g --request POST 'https://api.sciener.com/oauth2/token' \
--header 'Content-Type: application/x-www-form-urlencoded' \
--data-urlencode 'clientId=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx' \
--data-urlencode 'clientSecret=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx' \
--data-urlencode 'username=+8618966498228' \
--data-urlencode 'password=e10adc3949ba59abbe56e057f20f883e'
```

### 2 Request parameters

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| clientId | String | Y | clientId from [Create application](https://euopen.ttlock.com/CreateApplication) |
| clientSecret | String | Y | clientSecret from [Create application](https://euopen.ttlock.com/CreateApplication) |
| username | String | Y | username you used to login TTLock APP，or The prefixed username return by cloud api: [User register](https://euopen.ttlock.com/document/doc?urlName=cloud/user/registerEn.html). Notice：please do not use your open platform's developer account. |
| password | String | Y | Password(32 chars, low case, **md5 encrypted**) |

### 3 Response and example

| Name | Type | Request parameters |
| --- | --- | --- |
| access\_token | String | Access token |
| uid | Int | User id |
| expires\_in | Int | Expire time of access token, default validity of 90 days, in second. |
| refresh\_token | String | Refresh token |

```
{
    "access_token": "39caac89b0b51c980aa61ad4264b693b", 
    "uid": 2340,
    "refresh_token": "1bd2a21a7df889630f444364813738d7",
    "expires_in": 7776000,
}
```
