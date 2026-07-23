<!-- Source: https://euopen.ttlock.com/document/doc?urlName=cloud%2Fgroup%2FaddEn.html -->

# Add Group

**`https://api.sciener.com/v3/group/add`**

Add a group, a group belongs to a user, you can add locks and ekeys to a group and query locks and ekeys by group.
You can request [Init lock](https://euopen.ttlock.com/document/doc?urlName=cloud/lock/initializeEn.html) API with parameter `groupId` to add the lock into a group, or you can [set the group of a lock](https://euopen.ttlock.com/document/doc?urlName=cloud/group/setLockGroupEn.html) at any time.
Then you can [query your locks](https://euopen.ttlock.com/document/doc?urlName=cloud/lock/listEn.html) and [ekeys](https://euopen.ttlock.com/document/doc?urlName=cloud/ekey/listEn.html) by group.

### 1 Request example

`POST, ContentType:application/x-www-form-urlencoded`

```
curl --location -g --request POST 'https://api.sciener.com/v3/group/add' \
--data-urlencode 'clientId=4773aa036f7f49c68d876bb4be85c80c' \
--data-urlencode 'accessToken=dfd5489d0cee31f0bdfaf59d0d42d71f' \
--data-urlencode 'name=The 4th floor' \
--data-urlencode 'date=1625025703000'
```

### 2 Request parameters

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| clientId | String | Y | client\_id from [Create application](https://euopen.ttlock.com/CreateApplication) |
| accessToken | String | Y | Access token，refer to: [Get access token](https://euopen.ttlock.com/document/doc?urlName=cloud/oauth2/getAccessTokenEn.html) |
| name | String | Y | Group name |
| date | Long | Y | Current time (timestamp in millisecond) |

### 3 Response and example

| Parameter | Type | Description |
| --- | --- | --- |
| groupId | Int | Group ID |

```
{
    "groupId": 1342
}
```
