<!-- Source: https://euopen.ttlock.com/document/doc?urlName=cloud%2Foauth2%2FrefreshAccessTokenEn.html -->

# Refresh access token

**`https://api.sciener.com/oauth2/token`**

When access token expired, you can refresh it by this api, the refresh\_token is returned by [Get Access Token](https://euopen.ttlock.com/document/doc?urlName=cloud/oauth2/getAccessTokenEn.html), the valid period of refresh token is 10 years since it's created.

### 1 Request example

`POST, ContentType:application/x-www-form-urlencoded`

```
curl --location -g --request POST 'https://api.sciener.com/oauth2/token' \
--header 'Content-Type: application/x-www-form-urlencoded' \
--data-urlencode 'clientId=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx' \
--data-urlencode 'clientSecret=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx' \
--data-urlencode 'grant_type=refresh_token' \
--data-urlencode 'refresh_token=1bd2a21a7df889630f444364813738d7'
```

### 2 Request parameters

| Name | Required | Parameter value | Description |
| --- | --- | --- | --- |
| clientId | Y |  | clientId from [Create application](https://euopen.ttlock.com/CreateApplication) |
| clientSecret | Y |  | clientSecret from [Create application](https://euopen.ttlock.com/CreateApplication) |
| grant\_type | Y | refresh\_token | grant type, value: refresh\_token |
| refresh\_token | Y |  | refresh token |

### 3 Response and example

| Name | Type | Request parameters |
| --- | --- | --- |
| access\_token | String | Access token |
| expires\_in | Int | Expire time of access token, in second. |
| refresh\_token | String | Refresh token |

```
{
    "access_token": "39caac89b0b51c980aa61ad4264b693b",
    "refresh_token": "1bd2a21a7df889630f444364813738d7",
    "expires_in": 7776000
}
```
