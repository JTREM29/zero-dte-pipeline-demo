# REST
## Options

### Quotes

**Endpoint:** `GET /v3/quotes/{optionsTicker}`

**Description:**

Retrieve historical quotes for a specified options contract over a defined time range. Each record provides bid and ask prices, sizes, exchange identifiers, and precise timestamps, reflecting the options market conditions at each captured moment. By examining this data, users can analyze price movements, evaluate market interest in specific strikes and expirations, and refine their options trading or research strategies.

Use Cases: Historical quote analysis, market interest evaluation, algorithmic backtesting, strategy refinement.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `optionsTicker` | string | Yes | The ticker symbol to get quotes for. |
| `timestamp` | string | No | Query by timestamp. Either a date with the format YYYY-MM-DD or a nanosecond timestamp. |
| `timestamp.gte` | string | No | Range by timestamp. |
| `timestamp.gt` | string | No | Range by timestamp. |
| `timestamp.lte` | string | No | Range by timestamp. |
| `timestamp.lt` | string | No | Range by timestamp. |
| `order` | string | No | Order results based on the `sort` field. |
| `limit` | integer | No | Limit the number of results returned, default is 1000 and max is 50000. |
| `sort` | string | No | Sort field used for ordering. |

## Response Attributes

| Field | Type | Description |
| --- | --- | --- |
| `next_url` | string | If present, this value can be used to fetch the next page of data. |
| `request_id` | string | A request id assigned by the server. |
| `results` | array[object] | An array of results containing the requested data. |
| `results[].ask_exchange` | integer | The ask exchange ID |
| `results[].ask_price` | number | The ask price. |
| `results[].ask_size` | number | The ask size. This represents the number of round lot orders at the given ask price. The normal round lot size is 100 shares. An ask size of 2 means there are 200 shares available to purchase at the given ask price. |
| `results[].bid_exchange` | integer | The bid exchange ID |
| `results[].bid_price` | number | The bid price. |
| `results[].bid_size` | number | The bid size. This represents the number of round lot orders at the given bid price. The normal round lot size is 100 shares. A bid size of 2 means there are 200 shares for purchase at the given bid price. |
| `results[].sequence_number` | integer | The sequence number represents the sequence in which quote events happened. These are increasing and unique per ticker symbol, but will not always be sequential (e.g., 1, 2, 6, 9, 10, 11). |
| `results[].sip_timestamp` | integer | The nanosecond accuracy SIP Unix Timestamp. This is the timestamp of when the SIP received this quote from the exchange which produced it. |
| `status` | string | The status of this request's response. |

## Sample Response

```json
{
  "next_url": "https://api.massive.com/v3/quotes/O:SPY241220P00720000?cursor=YXA9NzY5Nzg0NzAxJmFzPSZsaW1pdD0xMCZvcmRlcj1kZXNjJnNvcnQ9dGltZXN0YW1wJnRpbWVzdGFtcC5sdGU9MjAyMi0wMi0xN1QxNyUzQTI1JTNBMTMuMDA5MzU2MDMyWg",
  "request_id": "a47d1beb8c11b6ae897ab76cdbbf35a3",
  "results": [
    {
      "ask_exchange": 323,
      "ask_price": 282,
      "ask_size": 10,
      "bid_exchange": 316,
      "bid_price": 277.5,
      "bid_size": 1,
      "sequence_number": 789539218,
      "sip_timestamp": 1645119125346243600
    },
    {
      "ask_exchange": 301,
      "ask_price": 282,
      "ask_size": 1,
      "bid_exchange": 323,
      "bid_price": 277.5,
      "bid_size": 10,
      "sequence_number": 788994206,
      "sip_timestamp": 1645119118474271000
    }
  ],
  "status": "OK"
}
```
