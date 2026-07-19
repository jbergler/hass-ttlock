<!-- Source: https://euopen.ttlock.com/document/doc?urlName=cloud%2Fnb%2FgetPlatformIpAndPortEn.html -->

# Get NB-IoT Clound Server Info

**`https://api.sciener.com/v3/lock/getNbPlatformIpAndPort`**

When [initializing NB-IoT lock by APP SDK](https://euopen.ttlock.com/document/doc?urlName=appSdkV3/androidSdkDemoV3/androidExample/addLockEn.html) you have to write NB-IoT cloud server's IP and port into the lock, then the lock can connect to the NB-IoT cloud server. You have to choose specific IP and port for different NB-IoT operator.

After the lock is successfully [initialized by APP SDK](https://euopen.ttlock.com/document/doc?urlName=appSdkV3/androidSdkDemoV3/androidExample/addLockEn.html), request the cloud API: [Init lock](https://euopen.ttlock.com/document/doc?urlName=cloud/lock/initializeEn.html) to initialize the lock on the cloud server and register it to the NB-IoT cloud server.

In case you have not register to NB-IoT cloud server when initializing the lock, you can call APP SDK method to config the lock, then request cloud API: [Register NB-IoT lock](https://euopen.ttlock.com/document/doc?urlName=cloud/nb/registerEn.html) to register it to NB-IoT cloud server.

### 1 Request example

```
curl --location -g --request GET 'https://api.sciener.com/v3/lock/getNbPlatformIpAndPort?clientId=fd2ff35ee3d8424c8665c07b7b9a7f45&accessToken=780f7671b6e6be3010d9787f19207aca&date=1625025703000'
```

### 2 Request parameters

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| clientId | String | Y | client\_id from [Create application](https://euopen.ttlock.com/CreateApplication) |
| accessToken | String | Y | Access token，refer to: [Get access token](https://euopen.ttlock.com/document/doc?urlName=cloud/oauth2/getAccessTokenEn.html) |
| date | Long | Y | Current time (timestamp in millisecond) |

### 3 Response and example

| Parameter | Type | Description |
| --- | --- | --- |
| list | JSONArray | list of records |

The objects in the list

| Parameter | Type | Description |
| --- | --- | --- |
| nbOperator | String | NB-IoT operator：46011-China Telecom、46000-China mobile、21401-Vodafone、50212-Maxis |
| ip | String | NB-IoT operator Server IP |
| port | Int | NB-IoT operator Server Port |

```
{
    "list": [
        {
            "port": 5683,
            "ip": "117.60.157.137",
            "nbOperator": "46011"
        },
        {
            "port": 5683,
            "ip": "112.13.167.63",
            "nbOperator": "46000"
        },
        {
            "port": 8887,
            "ip": "47.89.184.209",
            "nbOperator": "21401"
        },
        {
            "port": 8887,
            "ip": "47.89.184.209",
            "nbOperator": "50212"
        }
    ]
}
```
