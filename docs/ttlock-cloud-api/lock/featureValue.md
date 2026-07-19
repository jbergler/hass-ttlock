<!-- Source: https://euopen.ttlock.com/document/doc?urlName=cloud%2Flock%2FfeatureValueEn.html -->

# Description of Feature Value

`featureValue` is a hexadecimal string in upper case, representing the features the lock have.

If you are building an APP base on our APP SDK, you can use the functions in APP SDK to judge if the lock have a specific feature.

If you have to do it in you server or web applications, refer to following details.

### **1、Rules**

Convert the hexadecimal string into binary, each bit represents one feature, if the bit is set then the lock have the specific feature.

For example a lock have a featureValue of：

`C2F44354CF1F3`

Convert it to binary:

`1100 00101111 01000100 00110101 01001100 11110001 11110011`

Finally switch the endian ：

`11001111 10001111 00110010 10101100 00100010 11110100 0011`

### **2、The features represented by each bit**

| bit | feature |
| --- | --- |
| 0 | Passcode |
| 1 | Card |
| 2 | Fingerprint |
| 3 | Wrist strap |
| 4 | Support configuration of auto lock time |
| 5 | Support clear passcode |
| 6 | Firmware upgrade |
| 7 | Management of passcodes |
| 8 | Support lock command |
| 9 | Hide or display the passcodes, you can control it. |
| 10 | Support unlocking via gateway |
| 11 | Support freeze and unfreeze lock |
| 12 | Support cyclic passcodes |
| 13 | Support door sensor |
| 14 | Support configuration of unlocking via gateway, turn on or turn off it. |
| 15 | Voice prompts management, turn on or turn off it. |
| 16 | NB-IoT |
| 17 | This bit is discarded. |
| 18 | Support query of super passcode |
| 19 | Support hotel card |
| 20 | The lock does not have clock chip |
| 21 | When the lock's Bluetooth is not broadcasting, you cannot unlock via APP. |
| 22 | Passage mode |
| 23 | When you have set auto lock of passage mode, turn off auto lock is supported |
| 24 | Wireless keypad |
| 25 | Support time config of of the light. |
| 26 | Support backlist of hotel card |
| 27 | Support ID card |
| 28 | Tamper Alert can be turn on and turn off. |
| 29 | Reset Button can be turn on and turn off. |
| 30 | Privacy Lock can be turn on and turn off. |
| 31 | This bit is not used |
| 32 | Support deadlock |
| 33 | Support exception of passage mode |
| 34 | Support cyclic card and fingerprint |
| 35 | Privacy lock can be control by APP |
| 36 | Support setting of open direction |
| 37 | Support Finger vein |
| 38 | Telink bluetooth chip |
| 39 | NB-IoT activate mode can be configed |
| 40 | Support recovery of cyclic passcode |
| 41 | Support wireless keyfob (remote control) |
| 42 | Support query of accessory battery level |
| 43 | Configuration of sound volume and language is supported |
| 44 | Support QR code |
| 45 | Support unknown status of door sensor |
| 46 | Auto unlock in passage mode |
| 47 | Support adding fingerprint via gateway |
| 48 | Support Miaxis fingerprint data |
| 49 | Support Syno fingerprint data |
| 50 | Support wireless door sensor |
| 51 | Support alert when door is not locked |
| 53 | Support 3D face |
| 55 | Support CPU card |
| 56 | Support WiFi |
| 58 | WiFi lock which support fixed IP address |
| 60 | Support incomplete keyboard passcode |
| 63 | Support dual certification |
| 67 | Support Xmsilicon visual intercom |
| 69 | Support Zhiantec face module |
| 70 | Support plam vien |
| 74 | Support single QR code |
| 75 | Support Xiaocao visual intercom |
| 77 | Support third-party Bluetooth device access |
| 83 | Support WiFi power saving time range configuration |
| 84 | Support multi-function wireless keyboard |
| 108 | Supports custom QR codes |
| 110 | Support Yisheng face module |
| Other | Other bits is not used at the moment |

### 3、Code example

We only provide code example in Java and javascript, it will be appreciated if you can provide code in other languages, thanks in advance.

For example, a lock's featureValue is: C2F44354CF1F3, following codes judge if the lock have fingerprint feature.

##### 3.1、JAVA

```
import java.math.BigInteger;
public class HelloWorld {
    
    public static void main(String []args) {
        
        //Replace the corresponding lock feature value
        BigInteger bigInteger = new BigInteger("C2F44354CF1F3", 16);
        //input the number of bits to be detected
        System.out.println(bigInteger.testBit(53));
        
    }
}
```

##### 3.2、Javascript

```
  /**
* Equivalent to Java BigInteger.testBit(bitIndex)
* @param {string} hexStr: Hexadecimal string
* @param {number} bitIndex: Bit index (starting from 0)
* @returns {boolean}: Whether the bit is 1
 */
function testBitFromHex(hexStr, bitIndex) {
    const bigNum = BigInt(`0x${hexStr}`);
    const mask = 1n << BigInt(bitIndex);
    return (bigNum & mask) !== 0n;
}

// replace the corresponding lock feature value and input the number of bits to be detected.
const res = testBitFromHex("C2F44354CF1F3", 2);
console.log(res);
```
