<!-- Source: https://euopen.ttlock.com/document/doc?urlName=cloud%2FerrorCodeEn.html -->

# API Error Codes

# 1、Account、Rights and common erros

| error code | description |
| --- | --- |
| 1 | failed or means no |
| 10000 | client\_id does not exist |
| 10001 | invalid client，wrong client\_id or client\_secret |
| 10003 | token does not exist |
| 10004 | token is invalide or revoked |
| 10007 | invalid account or invalid password |
| 10011 | invalid refresh\_token |
| 20002 | not lock admin |
| 30002 | invalid username, only English character and digits is allowed |
| 30003 | existing registered users |
| 30004 | invalid userid to delete |
| 30005 | password must be md5 encrypted |
| 30006 | exceeds the restrictions of API call number |
| 80000 | date must be current time, in 5 minutes |
| 80002 | invalid json format |
| 90000 | internal server error |
| -3 | Invalid Parameter |
| -2018 | Permission Denied, many interfaces only allow the lock's top administrator or authorised administrator to request them, some interfaces only require a valid regular key user, please use an access token obtained from a legitimate user's account number and password to request the interface. |
| -4063 | Please delete/transfer all yours locks first |

# 2、Lock related errors

| error code | description |
| --- | --- |
| -1003 | Lock does not exist |
| -2025 | Frozen lock. Can not operate on it now |
| -3011 | Cannot Transfer Lock(s) to Yourself |
| -4043 | The function is not supported for this lock |
| -4056 | Run out of memory |
| -4067 | NB Device is not registered |
| -4082 | Auto locking period invalid |

# 3、Ekey related errors

| error code | description |
| --- | --- |
| -1008 | eKey does not exist |
| -1016 | An identical Name exists. Please choose a different Name. |
| -1018 | This Group does not exist |
| -1027 | Cant send eKey to this account which has been bound to another account |
| -2019 | You cannot send an eKey to Yourself |
| -2020 | You cannot send an eKey to the Admin |
| -2023 | Can't change the time period now |
| -4064 | Failed. The eKey can only be sent to a registered account |

# 4、Passcode related errors

| error code | description |
| --- | --- |
| -1007 | No password data of this lock |
| -2009 | Invalid Password |
| -3006 | Invalid Passcode. Passcode should be between 6 - 9 Digits in length |
| -3007 | The same passcode already exists. Please use another one |
| -3008 | A Passcode that has never been used on the Lock cannot be changed |
| -3009 | There is NO SPACE to store Customized Passcodes. Please Delete Un-Used Customized Passcodes and try again |

# 5、Gateway and WiFi lock related errors

| error code | description |
| --- | --- |
| -2012 | The Lock is not connected to any Gateway. |
| -3002 | The gateway is offline. Please check and try again. |
| -3003 | The gateway is busy. Please try again later. |
| -3016 | Cannot Transfer Gateway(s) to Yourself. |
| -3034 | Network not configed. Please config the network and try again. |
| -3035 | Wifi lock is in power saving mode, please turn off power saving and try again. |
| -3036 | The lock is offline. Please check and try again. |
| -3037 | The lock is busy. Please try again later. |
| -4037 | No such Gateway exists. |

# 6、IC card and Fingerprint related errors

| error code | description |
| --- | --- |
| -1021 | This IC Card does not exist |
| -1023 | This Fingerprint does not exist |
