# REST
## Forex

### Single Ticker Snapshot

**Endpoint:** `GET /v2/snapshot/locale/global/markets/forex/tickers/{ticker}`

**Description:**

Retrieve the most recent market data snapshot for a single ticker. This endpoint consolidates the latest quote and aggregated data (minute, day, and previous day) for the specified ticker. Snapshot data is cleared at 12:00 AM EST and begins updating as exchanges report new information. By focusing on a single ticker, users can closely monitor real-time developments and incorporate up-to-date information into trading strategies, alerts, or currency-level reporting.

Use Cases: Focused monitoring, real-time analysis, price alerts, investor relations.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `ticker` | string | Yes | The forex ticker. |

## Response Attributes

| Field | Type | Description |
| --- | --- | --- |
| `status` | string | The status of this request's response. |
| `request_id` | string | A request id assigned by the server. |
| `ticker` | object | Contains the requested snapshot data for the specified ticker. |
| `ticker.day` | object | The most recent daily bar for this ticker. |
| `ticker.fmv` | number | Fair market value is only available on Business plans. It is our proprietary algorithm to generate a real-time, accurate, fair market value of a tradable security. For more information, <a rel="nofollow" target="_blank" href="https://massive.com/contact">contact us</a>. |
| `ticker.lastQuote` | object | The most recent quote for this ticker. |
| `ticker.min` | object | The most recent minute bar for this ticker. |
| `ticker.prevDay` | object | The previous day's bar for this ticker. |
| `ticker.ticker` | string | The exchange symbol that this item is traded under. |
| `ticker.todaysChange` | number | The value of the change from the previous day. |
| `ticker.todaysChangePerc` | number | The percentage change since the previous day. |
| `ticker.updated` | integer | The last updated timestamp. |

## Sample Response

```json
{
  "request_id": "ad76e76ce183002c5937a7f02dfebde4",
  "status": "OK",
  "ticker": {
    "day": {
      "c": 1.18403,
      "h": 1.1906,
      "l": 1.18001,
      "o": 1.18725,
      "v": 83578
    },
    "lastQuote": {
      "a": 1.18403,
      "b": 1.18398,
      "i": 0,
      "t": 1606163759000,
      "x": 48
    },
    "min": {
      "c": 1.18396,
      "h": 1.18423,
      "l": 1.1838,
      "n": 85,
      "o": 1.18404,
      "t": 1684422000000,
      "v": 41
    },
    "prevDay": {
      "c": 1.18724,
      "h": 1.18727,
      "l": 1.18725,
      "o": 1.18725,
      "v": 5,
      "vw": 0
    },
    "ticker": "C:EURUSD",
    "todaysChange": -0.00316,
    "todaysChangePerc": -0.27458312,
    "updated": 1606163759000
  }
}
```
