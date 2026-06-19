# News Monitoring Pipeline

Cron-friendly Python pipeline for monitoring smartphone news sources, enriching article summaries, and writing new items to Google Sheets.

The project was designed for an editorial workflow where a human had to repeatedly check source sites, identify new relevant articles, translate short summaries, and copy results into a shared table.

## What It Does

- reads GSMArena news from RSS;
- reads PhoneArena news from XML sitemaps rather than Cloudflare-protected HTML pages;
- uses Google News sitemap titles for better PhoneArena headlines;
- converts source timestamps to Moscow time consistently;
- deduplicates against the existing Google Sheet;
- translates/enriches English descriptions into Russian text;
- writes new rows to Google Sheets in batches.

## Why RSS/XML

The pipeline intentionally prefers stable machine-readable sources:

- GSMArena provides a public RSS feed with article URLs and publication dates;
- PhoneArena exposes monthly XML sitemaps and a Google News sitemap;
- sitemap/RSS parsing is less brittle than scraping redesigned HTML pages;
- XML endpoints are also less likely to hit browser-only anti-bot checks.

## State And Deduplication

The Google Sheet is the source of truth.

On each run, the script reads existing rows, builds:

- a set of already-seen URLs;
- per-source cutoff timestamps based on the newest row for each source.

This avoids local state drift. If the sheet is edited manually, the next run uses the sheet as the current state.

## Configuration

Install dependencies:

```bash
python -m pip install -r requirements.txt
```

Required Google setup:

- enable Google Sheets API and Google Drive API;
- create a service account;
- share the target spreadsheet with the service account email;
- store the JSON key outside Git.

Environment variables:

| Variable | Purpose |
|---|---|
| `GOOGLE_APPLICATION_CREDENTIALS` | Path to service-account JSON. Defaults to `credentials.json` |
| `NEWS_SCRAPER_SPREADSHEET_ID` | Preferred target spreadsheet ID |
| `NEWS_SCRAPER_SPREADSHEET_NAME` | Fallback spreadsheet name if ID is empty |
| `NEWS_SCRAPER_WORKSHEET_NAME` | Target worksheet name. Defaults to `News` |

Run:

```bash
NEWS_SCRAPER_SPREADSHEET_ID='...' \
NEWS_SCRAPER_WORKSHEET_NAME='News' \
GOOGLE_APPLICATION_CREDENTIALS='/secure/path/service-account.json' \
python news_scraper.py
```

Cron example:

```cron
*/5 * * * * cd /opt/news-monitoring-pipeline && /usr/bin/python3 news_scraper.py >> cron.log 2>&1
```

## Design Notes

This pipeline focuses on operational reliability: stable RSS/XML inputs, explicit Google Sheets configuration, timezone-safe timestamps, deduplication against the destination table, and batch writes to reduce API calls. The runtime model is intentionally simple: one script, environment-based configuration, and cron-compatible execution.
