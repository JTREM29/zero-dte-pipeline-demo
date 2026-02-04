# REST
## Crypto

### Single Ticker Snapshot

**Endpoint:** `GET /v2/snapshot/locale/global/markets/crypto/tickers/{ticker}`

**Description:**

Retrieve the most recent market data snapshot for a single ticker. This endpoint consolidates the latest trade and aggregated data (minute, day, and previous day) for the specified ticker. Snapshot data is cleared at 12:00 AM EST and begins updating as exchanges report new information. By focusing on a single ticker, users can closely monitor real-time developments and incorporate up-to-date information into trading strategies, alerts, or crypto-level reporting.

Use Cases: Focused monitoring, real-time analysis, price alerts, investor relations.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `ticker` | string | Yes | Ticker of the snapshot |

## Response Attributes

| Field | Type | Description |
| --- | --- | --- |
| `status` | string | The status of this request's response. |
| `request_id` | string | A request id assigned by the server. |
| `ticker` | object | Contains the requested snapshot data for the specified ticker. |
| `ticker.day` | object | The most recent daily bar for this ticker. |
| `ticker.fmv` | number | Fair market value is only available on Business plans. It is our proprietary algorithm to generate a real-time, accurate, fair market value of a tradable security. For more information, <a rel="nofollow" target="_blank" href="https://massive.com/contact">contact us</a>. |
| `ticker.lastTrade` | object | The most recent trade for this ticker. |
| `ticker.min` | object | The most recent minute bar for this ticker. |
| `ticker.prevDay` | object | The previous day's bar for this ticker. |
| `ticker.ticker` | string | The exchange symbol that this item is traded under. |
| `ticker.todaysChange` | number | The value of the change from the previous day. |
| `ticker.todaysChangePerc` | number | The percentage change since the previous day. |
| `ticker.updated` | integer | The last updated timestamp. |

## Sample Response

```json
{
  "request_id": "ad92e92ce183112c593717f00dfebd2c",
  "status": "OK",
  "ticker": {
    "day": {
      "c": 16260.85,
      "h": 16428.4,
      "l": 15830.4,
      "o": 16418.07,
      "v": 105008.84231068,
      "vw": 0
    },
    "lastTrade": {
      "c": [
        2
      ],
      "i": "464569520",
      "p": 16242.31,
      "s": 0.001933,
      "t": 1605294230780,
      "x": 4
    },
    "min": {
      "c": 16235.1,
      "h": 16264.29,
      "l": 16129.3,
      "n": 558,
      "o": 16257.51,
      "t": 1684428960000,
      "v": 19.30791925,
      "vw": 0
    },
    "prevDay": {
      "c": 16399.24,
      "h": 16418.07,
      "l": 16399.24,
      "o": 16418.07,
      "v": 0.99167108,
      "vw": 16402.6893
    },
    "ticker": "X:BTCUSD",
    "todaysChange": -156.93,
    "todaysChangePerc": -0.956935,
    "updated": 1605330008999
  }
}
```
