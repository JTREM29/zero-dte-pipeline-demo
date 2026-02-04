# REST
## Crypto

### Full Market Snapshot

**Endpoint:** `GET /v2/snapshot/locale/global/markets/crypto/tickers`

**Description:**

Retrieve a comprehensive snapshot of the crypto market in a single response. This endpoint consolidates key information like pricing, volume, and trade activity to provide a full-market-snapshot view, eliminating the need for multiple queries. Snapshot data is cleared daily at 12:00 AM EST and begins to repopulate as exchanges report new data, which can start as early as 4:00 AM EST. By accessing all tickers at once, users can efficiently monitor broad market conditions, perform bulk analyses, and power applications that require complete, current market information.

Use Cases: Market overview, bulk data processing, heat maps/dashboards, automated monitoring.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `tickers` | array | No | A case-sensitive comma separated list of tickers to get snapshots for. For example, X:BTCUSD, X:ETHBTC, and X:BOBAUSD. Empty string defaults to querying all tickers. |

## Response Attributes

| Field | Type | Description |
| --- | --- | --- |
| `status` | string | The status of this request's response. |
| `tickers` | array[object] | An array of snapshot data for the specified tickers. |
| `tickers[].day` | object | The most recent daily bar for this ticker. |
| `tickers[].fmv` | number | Fair market value is only available on Business plans. It is our proprietary algorithm to generate a real-time, accurate, fair market value of a tradable security. For more information, <a rel="nofollow" target="_blank" href="https://massive.com/contact">contact us</a>. |
| `tickers[].lastTrade` | object | The most recent trade for this ticker. |
| `tickers[].min` | object | The most recent minute bar for this ticker. |
| `tickers[].prevDay` | object | The previous day's bar for this ticker. |
| `tickers[].ticker` | string | The exchange symbol that this item is traded under. |
| `tickers[].todaysChange` | number | The value of the change from the previous day. |
| `tickers[].todaysChangePerc` | number | The percentage change since the previous day. |
| `tickers[].updated` | integer | The last updated timestamp. |

## Sample Response

```json
{
  "status": "OK",
  "tickers": [
    {
      "day": {
        "c": 0.296,
        "h": 0.59714,
        "l": 0.23706,
        "o": 0.28,
        "v": 4097699.5691991993,
        "vw": 0
      },
      "lastTrade": {
        "c": [
          1
        ],
        "i": 413131,
        "p": 0.293,
        "s": 13.6191,
        "t": 1605292686010,
        "x": 17
      },
      "min": {
        "c": 0.296,
        "h": 0.296,
        "l": 0.294,
        "n": 2,
        "o": 0.296,
        "t": 1684427880000,
        "v": 123.4866,
        "vw": 0
      },
      "prevDay": {
        "c": 0.281,
        "h": 0.59714,
        "l": 0.23706,
        "o": 0.27,
        "v": 6070178.786154971,
        "vw": 0.4076
      },
      "ticker": "X:FSNUSD",
      "todaysChange": 0.012,
      "todaysChangePerc": 4.270463,
      "updated": 1605330008999
    }
  ]
}
```
