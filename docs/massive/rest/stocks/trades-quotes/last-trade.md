# REST
## Stocks

### Last Trade

**Endpoint:** `GET /v2/last/trade/{stocksTicker}`

**Description:**

Retrieve the latest available trade for a specified stock ticker, including details such as price, size, exchange, and timestamp. This endpoint supports monitoring recent trading activity and updating dashboards or applications with the most current trade information, providing timely insights into ongoing market conditions.

Use Cases: Trade monitoring, price updates, market snapshot.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `stocksTicker` | string | Yes | Specify a case-sensitive ticker symbol. For example, AAPL represents Apple Inc. |

## Response Attributes

| Field | Type | Description |
| --- | --- | --- |
| `request_id` | string | A request id assigned by the server. |
| `results` | object | N/A |
| `results.T` | string | The exchange symbol that this item is traded under. |
| `results.c` | array[integer] | A list of condition codes. |
| `results.e` | integer | The trade correction indicator. |
| `results.f` | integer | The nanosecond accuracy TRF(Trade Reporting Facility) Unix Timestamp. This is the timestamp of when the trade reporting facility received this message. |
| `results.i` | string | The Trade ID which uniquely identifies a trade. These are unique per combination of ticker, exchange, and TRF. For example: A trade for AAPL executed on NYSE and a trade for AAPL executed on NASDAQ could potentially have the same Trade ID. |
| `results.p` | number | The price of the trade. This is the actual dollar value per whole share of this trade. A trade of 100 shares with a price of $2.00 would be worth a total dollar value of $200.00. |
| `results.q` | integer | The sequence number represents the sequence in which message events happened. These are increasing and unique per ticker symbol, but will not always be sequential (e.g., 1, 2, 6, 9, 10, 11). |
| `results.r` | integer | The ID for the Trade Reporting Facility where the trade took place. |
| `results.s` | number | The size of a trade (also known as volume). |
| `results.t` | integer | The nanosecond accuracy SIP Unix Timestamp. This is the timestamp of when the SIP received this message from the exchange which produced it. |
| `results.x` | integer | The exchange ID. See <a href="https://massive.com/docs/rest/stocks/market-operations/exchanges" alt="Exchanges">Exchanges</a> for Massive's mapping of exchange IDs. |
| `results.y` | integer | The nanosecond accuracy Participant/Exchange Unix Timestamp. This is the timestamp of when the quote was actually generated at the exchange. |
| `results.z` | integer | There are 3 tapes which define which exchange the ticker is listed on. These are integers in our objects which represent the letter of the alphabet. Eg: 1 = A, 2 = B, 3 = C. * Tape A is NYSE listed securities * Tape B is NYSE ARCA / NYSE American * Tape C is NASDAQ |
| `status` | string | The status of this request's response. |

## Sample Response

```json
{
  "request_id": "f05562305bd26ced64b98ed68b3c5d96",
  "results": {
    "T": "AAPL",
    "c": [
      37
    ],
    "f": 1617901342969796400,
    "i": "118749",
    "p": 129.8473,
    "q": 3135876,
    "r": 202,
    "s": 25,
    "t": 1617901342969834000,
    "x": 4,
    "y": 1617901342968000000,
    "z": 3
  },
  "status": "OK"
}
```
