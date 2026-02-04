# REST
## Crypto

### Last Trade

**Endpoint:** `GET /v1/last/crypto/{from}/{to}`

**Description:**

Retrieve the most recent trade details for a specified cryptocurrency pair, including price, size, timestamp, exchange, and conditions. This endpoint delivers up-to-date market information, enabling real-time monitoring, rapid decision-making, and integration into crypto trading or analytics tools.

Use Cases: Real-time market monitoring, algorithmic trading, analytical insights, application development.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `from` | string | Yes | The "from" symbol of the pair. |
| `to` | string | Yes | The "to" symbol of the pair. |

## Response Attributes

| Field | Type | Description |
| --- | --- | --- |
| `last` | object | Contains the requested trade data for the specified cryptocurrency pair. |
| `last.conditions` | array[integer] | A list of condition codes. |
| `last.exchange` | integer | The exchange that this crypto trade happened on.   See <a href="https://massive.com/docs/rest/crypto/market-operations/exchanges">Exchanges</a> for a mapping of exchanges to IDs. |
| `last.price` | number | The price of the trade. This is the actual dollar value per whole share of this trade. A trade of 100 shares with a price of $2.00 would be worth a total dollar value of $200.00. |
| `last.size` | number | The size of a trade (also known as volume). |
| `last.timestamp` | integer | The Unix millisecond timestamp. |
| `request_id` | string | A request id assigned by the server. |
| `status` | string | The status of this request's response. |
| `symbol` | string | The symbol pair that was evaluated from the request. |

## Sample Response

```json
{
  "last": {
    "conditions": [
      1
    ],
    "exchange": 4,
    "price": 16835.42,
    "size": 0.006909,
    "timestamp": 1605560885027
  },
  "request_id": "d2d779df015fe2b7fbb8e58366610ef7",
  "status": "success",
  "symbol": "BTC-USD"
}
```
