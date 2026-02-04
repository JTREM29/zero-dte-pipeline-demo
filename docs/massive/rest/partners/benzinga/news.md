# REST
## Partners

### Real-time Benzinga News

**Endpoint:** `GET /benzinga/v2/news`

**Description:**

Retrieve real-time structured, timestamped news articles from Benzinga, including headlines, full-text content, tickers, categories, and more. Each article entry contains metadata such as author, publication time, and topic channels, as well as optional elements like teaser summaries, article body text, and images. Articles can be filtered by ticker and time, and are returned in a consistent format for easy parsing and integration. This endpoint is ideal for building alerting systems, autonomous risk analysis, and sentiment-driven trading strategies.

Use Cases: Market news feeds, alerting systems, portfolio monitoring, dashboards.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `published` | string | No | The timestamp (formatted as an ISO 8601 timestamp) when the news article was originally published. Value must be an integer timestamp in seconds or formatted 'yyyy-mm-dd'. |
| `published.gt` | string | No | Filter greater than the value. Value must be an integer timestamp in seconds or formatted 'yyyy-mm-dd'. |
| `published.gte` | string | No | Filter greater than or equal to the value. Value must be an integer timestamp in seconds or formatted 'yyyy-mm-dd'. |
| `published.lt` | string | No | Filter less than the value. Value must be an integer timestamp in seconds or formatted 'yyyy-mm-dd'. |
| `published.lte` | string | No | Filter less than or equal to the value. Value must be an integer timestamp in seconds or formatted 'yyyy-mm-dd'. |
| `channels` | string | No | Filter for arrays that contain the value. |
| `channels.all_of` | string | No | Filter for arrays that contain all of the values. Multiple values can be specified by using a comma separated list. |
| `channels.any_of` | string | No | Filter for arrays that contain any of the values. Multiple values can be specified by using a comma separated list. |
| `tags` | string | No | Filter for arrays that contain the value. |
| `tags.all_of` | string | No | Filter for arrays that contain all of the values. Multiple values can be specified by using a comma separated list. |
| `tags.any_of` | string | No | Filter for arrays that contain any of the values. Multiple values can be specified by using a comma separated list. |
| `author` | string | No | The name of the journalist or entity that authored the news article. |
| `author.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. |
| `author.gt` | string | No | Filter greater than the value. |
| `author.gte` | string | No | Filter greater than or equal to the value. |
| `author.lt` | string | No | Filter less than the value. |
| `author.lte` | string | No | Filter less than or equal to the value. |
| `stocks` | string | No | Filter for arrays that contain the value. |
| `stocks.all_of` | string | No | Filter for arrays that contain all of the values. Multiple values can be specified by using a comma separated list. |
| `stocks.any_of` | string | No | Filter for arrays that contain any of the values. Multiple values can be specified by using a comma separated list. |
| `tickers` | string | No | Filter for arrays that contain the value. |
| `tickers.all_of` | string | No | Filter for arrays that contain all of the values. Multiple values can be specified by using a comma separated list. |
| `tickers.any_of` | string | No | Filter for arrays that contain any of the values. Multiple values can be specified by using a comma separated list. |
| `limit` | integer | No | Limit the maximum number of results returned. Defaults to '100' if not specified. The maximum allowed limit is '50000'. |
| `sort` | string | No | A comma separated list of sort columns. For each column, append '.asc' or '.desc' to specify the sort direction. The sort column defaults to 'published' if not specified. The sort order defaults to 'desc' if not specified. |

## Response Attributes

| Field | Type | Description |
| --- | --- | --- |
| `next_url` | string | If present, this value can be used to fetch the next page. |
| `request_id` | string | A request id assigned by the server. |
| `results` | array[object] | The results for this request. |
| `results[].author` | string | The name of the journalist or entity that authored the news article. |
| `results[].benzinga_id` | integer | The identifer used by Benzinga for this record. |
| `results[].body` | string | The full text content of the news article. |
| `results[].channels` | array[string] | A list of categories or topics that the article belongs to (e.g., 'News', 'Price Target'). |
| `results[].images` | array[string] | A list of images associated with the article. |
| `results[].last_updated` | string | The timestamp (formatted as an ISO 8601 timestamp) when the news article was last updated in the system. |
| `results[].published` | string | The timestamp (formatted as an ISO 8601 timestamp) when the news article was originally published. |
| `results[].tags` | array[string] | A list of tags that describe the themes or content of the article. |
| `results[].teaser` | string | A short summary or lead-in to the news article's content. |
| `results[].tickers` | array[string] | A list of stock or crypto tickers mentioned in the article. |
| `results[].title` | string | The headline of the news article. |
| `results[].url` | string | The direct link to the source of the news article. |
| `status` | enum: OK | The status of this request's response. |

## Sample Response

```json
{
  "count": 1,
  "request_id": 1,
  "results": [
    {
      "author": "Adam Eckert",
      "benzinga_id": 39046904,
      "body": "<p><strong>CAVA Group Inc</strong> (NYSE:<a class=\"ticker\" href=\"https://www.benzinga.com/stock/CAVA#NYSE\">CAVA</a>) reported financial results for the first quarter of fiscal 2024 after market close on Tuesday. Here&#8217;s a look at the <a href=\"https://www.benzinga.com/pressreleases/24/05/b39046606/cava-group-reports-first-quarter-2024-results\">key metrics from the quarter</a>.</p>",
      "channels": [
        "earnings",
        "news",
        "restaurants",
        "after-hours center",
        "movers"
      ],
      "images": [
        "https://cdn.benzinga.com/files/imagecache/250x187xUP/images/story/2024/05/28/CAVA-group.jpeg",
        "https://cdn.benzinga.com/files/imagecache/1024x768xUP/images/story/2024/05/28/CAVA-group.jpeg",
        "https://cdn.benzinga.com/files/imagecache/2048x1536xUP/images/story/2024/05/28/CAVA-group.jpeg"
      ],
      "last_updated": "2024-05-28T20:27:42Z",
      "published": "2024-05-28T20:27:41Z",
      "tags": [
        "why it's moving"
      ],
      "teaser": "Cava&#39;s first-quarter revenue increased 30.3% year-over-year to $256.3 million, which beat the consensus estimate of $245.935 million, according to Benzinga Pro.",
      "tickers": [
        "CAVA"
      ],
      "title": "Cava Group Q1 Earnings: Revenue Beat, EPS Beat, Guidance Raise, Continued Investments In Scalable Infrastructure And More",
      "url": "https://www.benzinga.com/news/earnings/24/05/39046904/cava-group-q1-earnings-revenue-beat-eps-beat-guidance-raise-continued-investments-in-scalable-infra"
    }
  ],
  "status": "OK"
}
```
