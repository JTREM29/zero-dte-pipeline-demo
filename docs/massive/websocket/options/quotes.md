# WEBSOCKET
## Options

### Quotes

**Endpoint:** `WS /options/Q`

**Description:**

Stream quote data for specified options contracts via WebSocket. Each message delivers current best bid/ask prices, sizes, and associated metadata as they update, enabling users to monitor dynamic market conditions and inform trading decisions. Due to the high bandwidth and message rates associated with options quotes, users can subscribe to a maximum of 1,000 option contracts per connection.

Use Cases: Live monitoring, market analysis, trading decision support, dynamic interface updates.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `ticker` | string | Yes | Specify an option contract. You're only allowed to subscribe to 1,000 option contracts per connection. You can also use a comma separated list to subscribe to multiple option contracts. You can retrieve active options contracts from our [Options Contracts API](https://massive.com/docs/rest/options/contracts/all-contracts).  |

## Response Attributes

| Field | Type | Description |
| --- | --- | --- |
| `ev` | enum: Q | The event type. |
| `sym` | string | The ticker symbol for the given option contract. |
| `bx` | integer | The bid exchange ID. See <a target="_blank" href="https://massive.com/docs/rest/options/market-operations/exchanges" alt="Exchanges">Exchanges</a> for Massive's mapping of exchange IDs. |
| `ax` | integer | The ask exchange ID. See <a target="_blank" href="https://massive.com/docs/rest/options/market-operations/exchanges" alt="Exchanges">Exchanges</a> for Massive's mapping of exchange IDs. |
| `bp` | number | The bid price. |
| `ap` | number | The ask price. |
| `bs` | integer | The bid size. |
| `as` | integer | The ask size. |
| `t` | integer | The Timestamp in Unix MS. |
| `q` | integer | The sequence number represents the sequence in which trade events happened. These are increasing and unique per ticker symbol, but will not always be sequential (e.g., 1, 2, 6, 9, 10, 11). |

## Sample Response

```json
{
  "ev": "Q",
  "sym": "O:SPY241220P00720000",
  "bx": 302,
  "ax": 302,
  "bp": 9.71,
  "ap": 9.81,
  "bs": 17,
  "as": 24,
  "t": 1644506128351,
  "q": 844090872
}
```
