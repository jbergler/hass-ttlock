<!-- Source: https://euopen.ttlock.com/document/doc?urlName=cloud%2Ffingerprint%2FrenameEn.html -->

# Rename Fingerprint

**`https://api.sciener.com/v3/fingerprint/rename`**

Rename the Fingerprint.

### 1 HTTP Request example

`POST, ContentType:application/x-www-form-urlencoded`

```
curl --location -g --request POST 'https://api.sciener.com/v3/fingerprint/rename' \
--data-urlencode 'clientId=4773aa036f7f49c68d876bb4be85c80c' \
--data-urlencode 'accessToken=dfd5489d0cee31f0bdfaf59d0d42d71f' \
--data-urlencode 'lockId=163377' \
--data-urlencode 'fingerprintId=124242' \
--data-urlencode 'fingerprintName=Fingerprint of Mom' \
--data-urlencode 'date=1625025703000'
```

### 2 Request Parameter Description

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| clientId | String | Y | The app\_id which is assigned by system when you create an application |
| accessToken | String | Y | Access token |
| lockId | Int | Y | Lock ID |
| fingerprintId | Int | Y | fingerprint id |
| fingerprintName | String | Y | new fingerprint name |
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
