# REST
## Partners

### ETF Analytics

**Endpoint:** `GET /etf-global/v1/analytics`

**Description:**

Retrieve analytical metrics and calculated insights for global ETFs including performance data, risk measures, and derived analytics.

Use Cases: ETF performance evaluation, risk-reward assessment, quantitative screening, portfolio optimization and selection.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `composite_ticker` | string | No | The stock ticker symbol used to identify this ETF product on exchanges. |
| `composite_ticker.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. |
| `composite_ticker.gt` | string | No | Filter greater than the value. |
| `composite_ticker.gte` | string | No | Filter greater than or equal to the value. |
| `composite_ticker.lt` | string | No | Filter less than the value. |
| `composite_ticker.lte` | string | No | Filter less than or equal to the value. |
| `processed_date` | string | No | The date showing when ETF Global received and processed the data. Value must be formatted 'yyyy-mm-dd'. |
| `processed_date.gt` | string | No | Filter greater than the value. Value must be formatted 'yyyy-mm-dd'. |
| `processed_date.gte` | string | No | Filter greater than or equal to the value. Value must be formatted 'yyyy-mm-dd'. |
| `processed_date.lt` | string | No | Filter less than the value. Value must be formatted 'yyyy-mm-dd'. |
| `processed_date.lte` | string | No | Filter less than or equal to the value. Value must be formatted 'yyyy-mm-dd'. |
| `effective_date` | string | No | The date showing when the information was accurate or valid; some issuers, such as Vanguard, release their data on a delay, so the effective_date can be several weeks earlier than the processed_date. Value must be formatted 'yyyy-mm-dd'. |
| `effective_date.gt` | string | No | Filter greater than the value. Value must be formatted 'yyyy-mm-dd'. |
| `effective_date.gte` | string | No | Filter greater than or equal to the value. Value must be formatted 'yyyy-mm-dd'. |
| `effective_date.lt` | string | No | Filter less than the value. Value must be formatted 'yyyy-mm-dd'. |
| `effective_date.lte` | string | No | Filter less than or equal to the value. Value must be formatted 'yyyy-mm-dd'. |
| `risk_total_score` | number | No | ETF Global's proprietary Red Diamond overall risk assessment score for the ETF. Value must be a floating point number. |
| `risk_total_score.gt` | number | No | Filter greater than the value. Value must be a floating point number. |
| `risk_total_score.gte` | number | No | Filter greater than or equal to the value. Value must be a floating point number. |
| `risk_total_score.lt` | number | No | Filter less than the value. Value must be a floating point number. |
| `risk_total_score.lte` | number | No | Filter less than or equal to the value. Value must be a floating point number. |
| `reward_score` | number | No | ETF Global's proprietary Green Diamond score measuring the potential reward and return prospects of the ETF. Value must be a floating point number. |
| `reward_score.gt` | number | No | Filter greater than the value. Value must be a floating point number. |
| `reward_score.gte` | number | No | Filter greater than or equal to the value. Value must be a floating point number. |
| `reward_score.lt` | number | No | Filter less than the value. Value must be a floating point number. |
| `reward_score.lte` | number | No | Filter less than or equal to the value. Value must be a floating point number. |
| `quant_total_score` | number | No | ETF Global's comprehensive quantitative analysis score combining all quantitative factors. Value must be a floating point number. |
| `quant_total_score.gt` | number | No | Filter greater than the value. Value must be a floating point number. |
| `quant_total_score.gte` | number | No | Filter greater than or equal to the value. Value must be a floating point number. |
| `quant_total_score.lt` | number | No | Filter less than the value. Value must be a floating point number. |
| `quant_total_score.lte` | number | No | Filter less than or equal to the value. Value must be a floating point number. |
| `quant_grade` | string | No | Letter grade summarizing the ETF's overall quantitative assessment, where A = 71-100, B = 56-70, etc. |
| `quant_grade.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. |
| `quant_grade.gt` | string | No | Filter greater than the value. |
| `quant_grade.gte` | string | No | Filter greater than or equal to the value. |
| `quant_grade.lt` | string | No | Filter less than the value. |
| `quant_grade.lte` | string | No | Filter less than or equal to the value. |
| `quant_composite_technical` | number | No | Combined technical analysis score aggregating short, intermediate, and long-term technical factors. Value must be a floating point number. |
| `quant_composite_technical.gt` | number | No | Filter greater than the value. Value must be a floating point number. |
| `quant_composite_technical.gte` | number | No | Filter greater than or equal to the value. Value must be a floating point number. |
| `quant_composite_technical.lt` | number | No | Filter less than the value. Value must be a floating point number. |
| `quant_composite_technical.lte` | number | No | Filter less than or equal to the value. Value must be a floating point number. |
| `quant_composite_sentiment` | number | No | Overall market sentiment score combining put/call ratios, short interest, and implied volatility. Value must be a floating point number. |
| `quant_composite_sentiment.gt` | number | No | Filter greater than the value. Value must be a floating point number. |
| `quant_composite_sentiment.gte` | number | No | Filter greater than or equal to the value. Value must be a floating point number. |
| `quant_composite_sentiment.lt` | number | No | Filter less than the value. Value must be a floating point number. |
| `quant_composite_sentiment.lte` | number | No | Filter less than or equal to the value. Value must be a floating point number. |
| `quant_composite_behavioral` | number | No | Behavioral analysis score measuring investor psychology and market behavior patterns. Value must be a floating point number. |
| `quant_composite_behavioral.gt` | number | No | Filter greater than the value. Value must be a floating point number. |
| `quant_composite_behavioral.gte` | number | No | Filter greater than or equal to the value. Value must be a floating point number. |
| `quant_composite_behavioral.lt` | number | No | Filter less than the value. Value must be a floating point number. |
| `quant_composite_behavioral.lte` | number | No | Filter less than or equal to the value. Value must be a floating point number. |
| `quant_composite_fundamental` | number | No | Overall fundamental analysis score combining P/E, P/CF, P/B, and dividend yield metrics. Value must be a floating point number. |
| `quant_composite_fundamental.gt` | number | No | Filter greater than the value. Value must be a floating point number. |
| `quant_composite_fundamental.gte` | number | No | Filter greater than or equal to the value. Value must be a floating point number. |
| `quant_composite_fundamental.lt` | number | No | Filter less than the value. Value must be a floating point number. |
| `quant_composite_fundamental.lte` | number | No | Filter less than or equal to the value. Value must be a floating point number. |
| `quant_composite_global` | number | No | Overall global theme score combining sector and country analysis for macro investment views. Value must be a floating point number. |
| `quant_composite_global.gt` | number | No | Filter greater than the value. Value must be a floating point number. |
| `quant_composite_global.gte` | number | No | Filter greater than or equal to the value. Value must be a floating point number. |
| `quant_composite_global.lt` | number | No | Filter less than the value. Value must be a floating point number. |
| `quant_composite_global.lte` | number | No | Filter less than or equal to the value. Value must be a floating point number. |
| `quant_composite_quality` | number | No | Overall quality assessment score combining liquidity, diversification, and issuing firm factors. Value must be a floating point number. |
| `quant_composite_quality.gt` | number | No | Filter greater than the value. Value must be a floating point number. |
| `quant_composite_quality.gte` | number | No | Filter greater than or equal to the value. Value must be a floating point number. |
| `quant_composite_quality.lt` | number | No | Filter less than the value. Value must be a floating point number. |
| `quant_composite_quality.lte` | number | No | Filter less than or equal to the value. Value must be a floating point number. |
| `limit` | integer | No | Limit the maximum number of results returned. Defaults to '100' if not specified. The maximum allowed limit is '5000'. |
| `sort` | string | No | A comma separated list of sort columns. For each column, append '.asc' or '.desc' to specify the sort direction. The sort column defaults to 'composite_ticker' if not specified. The sort order defaults to 'asc' if not specified. |

## Response Attributes

| Field | Type | Description |
| --- | --- | --- |
| `next_url` | string | If present, this value can be used to fetch the next page. |
| `request_id` | string | A request id assigned by the server. |
| `results` | array[object] | The results for this request. |
| `results[].composite_ticker` | string | The stock ticker symbol used to identify this ETF product on exchanges. |
| `results[].effective_date` | string | The date showing when the information was accurate or valid; some issuers, such as Vanguard, release their data on a delay, so the effective_date can be several weeks earlier than the processed_date. |
| `results[].processed_date` | string | The date showing when ETF Global received and processed the data. |
| `results[].quant_composite_behavioral` | number | Behavioral analysis score measuring investor psychology and market behavior patterns. |
| `results[].quant_composite_fundamental` | number | Overall fundamental analysis score combining P/E, P/CF, P/B, and dividend yield metrics. |
| `results[].quant_composite_global` | number | Overall global theme score combining sector and country analysis for macro investment views. |
| `results[].quant_composite_quality` | number | Overall quality assessment score combining liquidity, diversification, and issuing firm factors. |
| `results[].quant_composite_sentiment` | number | Overall market sentiment score combining put/call ratios, short interest, and implied volatility. |
| `results[].quant_composite_technical` | number | Combined technical analysis score aggregating short, intermediate, and long-term technical factors. |
| `results[].quant_fundamental_div` | number | Fundamental analysis score based on dividend yields of the ETF's underlying securities. |
| `results[].quant_fundamental_pb` | number | Fundamental analysis score based on price-to-book value ratios of the ETF's holdings. |
| `results[].quant_fundamental_pcf` | number | Fundamental analysis score based on price-to-cash-flow ratios of the ETF's underlying assets. |
| `results[].quant_fundamental_pe` | number | Fundamental analysis score based on price-to-earnings ratios of the ETF's underlying holdings. |
| `results[].quant_global_country` | number | Quantitative score analyzing global country themes and country-specific market factors. |
| `results[].quant_global_sector` | number | Quantitative score analyzing global sector themes and sector-specific performance factors. |
| `results[].quant_grade` | string | Letter grade summarizing the ETF's overall quantitative assessment, where A = 71-100, B = 56-70, etc. |
| `results[].quant_quality_diversification` | number | Quality assessment score evaluating the diversification benefits and risk distribution of the ETF. |
| `results[].quant_quality_firm` | number | Quality assessment score evaluating the reputation and capabilities of the ETF's issuing firm. |
| `results[].quant_quality_liquidity` | number | Quality assessment score measuring the liquidity characteristics and trading ease of the ETF. |
| `results[].quant_sentiment_iv` | number | Market sentiment score derived from implied volatility levels in options markets. |
| `results[].quant_sentiment_pc` | number | Market sentiment score derived from put/call option ratios and options activity. |
| `results[].quant_sentiment_si` | number | Market sentiment score based on short interest levels and short selling activity. |
| `results[].quant_technical_it` | number | Intermediate-term technical analysis score evaluating medium-term price trends. |
| `results[].quant_technical_lt` | number | Long-term technical analysis score assessing extended price trend patterns. |
| `results[].quant_technical_st` | number | Short-term technical analysis score based on recent price movements and trading patterns. |
| `results[].quant_total_score` | number | ETF Global's comprehensive quantitative analysis score combining all quantitative factors. |
| `results[].reward_score` | number | ETF Global's proprietary Green Diamond score measuring the potential reward and return prospects of the ETF. |
| `results[].risk_country` | number | A component score assessing country-specific risks based on the ETF's geographic exposure. |
| `results[].risk_deviation` | number | A component score measuring how much the ETF deviates from expected performance. |
| `results[].risk_efficiency` | number | A component score assessing the operational efficiency and cost-effectiveness of the ETF. |
| `results[].risk_liquidity` | number | A component score measuring the liquidity risk and ease of trading the ETF. |
| `results[].risk_structure` | number | A component score evaluating risks related to the ETF's structural design and mechanics. |
| `results[].risk_total_score` | number | ETF Global's proprietary Red Diamond overall risk assessment score for the ETF. |
| `results[].risk_volatility` | number | A component score measuring the volatility risk of the ETF's price movements. |
| `status` | enum: OK | The status of this request's response. |

## Sample Response

```json
{
  "count": 1,
  "request_id": 1,
  "results": [
    {
      "composite_ticker": "SPY",
      "effective_date": "2025-09-19",
      "processed_date": "2025-09-19",
      "quant_composite_behavioral": 67.1535,
      "quant_composite_fundamental": 1.2,
      "quant_composite_global": 52.9,
      "quant_composite_quality": 75.9,
      "quant_composite_sentiment": 54.6,
      "quant_composite_technical": 79.7,
      "quant_fundamental_div": 4.7,
      "quant_fundamental_pb": 0,
      "quant_fundamental_pcf": 0,
      "quant_fundamental_pe": 0,
      "quant_global_country": 85.4,
      "quant_global_sector": 20.4,
      "quant_grade": "D",
      "quant_quality_diversification": 29.3,
      "quant_quality_firm": 98.3,
      "quant_quality_liquidity": 100,
      "quant_sentiment_iv": 23,
      "quant_sentiment_pc": 88.9,
      "quant_sentiment_si": 51.6,
      "quant_technical_it": 79,
      "quant_technical_lt": 78.7,
      "quant_technical_st": 83.1,
      "quant_total_score": 40.2,
      "reward_score": 3.12,
      "risk_country": 1.46,
      "risk_deviation": 7.68,
      "risk_efficiency": 1.85,
      "risk_liquidity": 2.5,
      "risk_structure": 2.37,
      "risk_total_score": 9.22,
      "risk_volatility": 4.66
    }
  ],
  "status": "OK"
}
```
