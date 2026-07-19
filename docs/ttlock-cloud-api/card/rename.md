<!-- Source: https://euopen.ttlock.com/document/doc?urlName=cloud%2Fcard%2FrenameEn.html -->

# Rename card

**`https://api.sciener.com/v3/identityCard/rename`**

Rename the card.

### 1 HTTP Request example

`POST, ContentType:application/x-www-form-urlencoded`

```
curl --location -g --request POST 'https://api.sciener.com/v3/identityCard/rename' \
--data-urlencode 'clientId=4773aa036f7f49c68d876bb4be85c80c' \
--data-urlencode 'accessToken=dfd5489d0cee31f0bdfaf59d0d42d71f' \
--data-urlencode 'lockId=163377' \
--data-urlencode 'cardId=124242' \
--data-urlencode 'cardName=Card for Mom' \
--data-urlencode 'date=1625025703000'
```

### 2 Request Parameter Description

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| clientId | String | Y | client\_id from [Create application](https://euopen.ttlock.com/CreateApplication) |
| accessToken | String | Y | Access token，refer to: [Get access token](https://euopen.ttlock.com/document/doc?urlName=cloud/oauth2/getAccessTokenEn.html) |
| lockId | Int | Y | Lock ID |
| cardId | Int | Y | Card ID，get from Cloud API: [Add card](https://euopen.ttlock.com/document/doc?urlName=cloud/card/addEn.html) |
| cardName | String | Y | New card name |
| date | Long | Y | Current time (timestamp in millisecond) |

### 3 Response

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
