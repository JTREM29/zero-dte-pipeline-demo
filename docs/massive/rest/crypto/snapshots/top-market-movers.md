# REST
## Crypto

### Top Market Movers

**Endpoint:** `GET /v2/snapshot/locale/global/markets/crypto/{direction}`

**Description:**

Retrieve snapshot data highlighting the top 20 gainers or losers in the crypto market. Gainers are stocks with the largest percentage increase since the previous day’s close, and losers are those with the largest percentage decrease. Snapshot data is cleared daily at 12:00 AM EST and begins repopulating as exchanges report new information. By focusing on these market movers, users can quickly identify significant price shifts and monitor evolving market dynamics.

Use Cases: Market movers identification, trading strategies, market sentiment analysis, portfolio adjustments.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `direction` | string | Yes | The direction of the snapshot results to return.  |

## Response Attributes

| Field | Type | Description |
| --- | --- | --- |
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
        "c": 0.0374,
        "h": 0.062377,
        "l": 0.01162,
        "o": 0.044834,
        "v": 27313165.159427017,
        "vw": 0
      },
      "lastTrade": {
        "c": [
          2
        ],
        "i": "517478762",
        "p": 0.0374,
        "s": 499,
        "t": 1604409649544,
        "x": 2
      },
      "min": {
        "c": 0.062377,
        "h": 0.062377,
        "l": 0.062377,
        "n": 2,
        "o": 0.062377,
        "t": 1684426740000,
        "v": 35420,
        "vw": 0
      },
      "prevDay": {
        "c": 0.01162,
        "h": 0.044834,
        "l": 0.01162,
        "o": 0.044834,
        "v": 53616273.36827199,
        "vw": 0.0296
      },
      "ticker": "X:DRNUSD",
      "todaysChange": 0.02578,
      "todaysChangePerc": 221.858864,
      "updated": 1605330008999
    }
  ]
}
```
