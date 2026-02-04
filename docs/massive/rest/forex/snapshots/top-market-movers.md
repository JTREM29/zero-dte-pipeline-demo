# REST
## Forex

### Top Market Movers

**Endpoint:** `GET /v2/snapshot/locale/global/markets/forex/{direction}`

**Description:**

Retrieve snapshot data highlighting the top 20 gainers or losers in the forex market. Gainers are stocks with the largest percentage increase since the previous day’s close, and losers are those with the largest percentage decrease. Snapshot data is cleared daily at 12:00 AM EST and begins repopulating as exchanges report new information. By focusing on these market movers, users can quickly identify significant price shifts and monitor evolving market dynamics.

Use Cases: Market movers identification, trading strategies, market sentiment analysis, portfolio adjustments.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `direction` | string | Yes | The direction of the snapshot results to return.  |

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
        "c": 0.886156,
        "h": 0.887111,
        "l": 0.8825327,
        "o": 0.8844732,
        "v": 1041
      },
      "lastQuote": {
        "a": 0.8879606,
        "b": 0.886156,
        "t": 1605283204000,
        "x": 48
      },
      "min": {
        "c": 0.886156,
        "h": 0.886156,
        "l": 0.886156,
        "n": 1,
        "o": 0.886156,
        "t": 1684422000000,
        "v": 1
      },
      "prevDay": {
        "c": 0.8428527,
        "h": 0.889773,
        "l": 0.8428527,
        "o": 0.8848539,
        "v": 1078,
        "vw": 0
      },
      "ticker": "C:PLNILS",
      "todaysChange": 0.0433033,
      "todaysChangePerc": 5.13770674,
      "updated": 1605330008999
    }
  ]
}
```
