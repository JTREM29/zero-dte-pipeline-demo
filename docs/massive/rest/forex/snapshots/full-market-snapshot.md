# REST
## Forex

### Full Market Snapshot

**Endpoint:** `GET /v2/snapshot/locale/global/markets/forex/tickers`

**Description:**

Retrieve a comprehensive snapshot of the forex market in a single response. This endpoint consolidates key information like pricing, volume, and quote activity to provide a full-market-snapshot view, eliminating the need for multiple queries. Snapshot data is cleared daily at 12:00 AM EST and begins to repopulate as exchanges report new data, which can start as early as 4:00 AM EST. By accessing all tickers at once, users can efficiently monitor broad market conditions, perform bulk analyses, and power applications that require complete, current market information.

Use Cases: Market overview, bulk data processing, heat maps/dashboards, automated monitoring.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `tickers` | array | No | A case-sensitive comma separated list of tickers to get snapshots for. For example, C:EURUSD, C:GBPCAD, and C:AUDINR. Empty string defaults to querying all tickers. |

## Response Attributes

| Field | Type | Description |
| --- | --- | --- |
| `status` | string | The status of this request's response. |
| `tickers` | array[object] | An array of snapshot data for the specified tickers. |
| `tickers[].day` | object | The most recent daily bar for this ticker. |
| `tickers[].fmv` | number | Fair market value is only available on Business plans. It is our proprietary algorithm to generate a real-time, accurate, fair market value of a tradable security. For more information, <a rel="nofollow" target="_blank" href="https://massive.com/contact">contact us</a>. |
| `tickers[].lastQuote` | object | The most recent quote for this ticker. |
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
        "c": 0.11778221,
        "h": 0.11812263,
        "l": 0.11766631,
        "o": 0.11797149,
        "v": 77794
      },
      "lastQuote": {
        "a": 0.11780678,
        "b": 0.11777952,
        "t": 1605280919000,
        "x": 48
      },
      "min": {
        "c": 0.117769,
        "h": 0.11779633,
        "l": 0.11773698,
        "n": 1,
        "o": 0.11778,
        "t": 1684422000000,
        "v": 202
      },
      "prevDay": {
        "c": 0.11797258,
        "h": 0.11797258,
        "l": 0.11797149,
        "o": 0.11797149,
        "v": 2,
        "vw": 0
      },
      "ticker": "C:HKDCHF",
      "todaysChange": -0.00019306,
      "todaysChangePerc": -0.1636482,
      "updated": 1605280919000
    }
  ]
}
```
