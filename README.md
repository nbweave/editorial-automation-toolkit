# Editorial Automation Toolkit

Automation utilities for editorial workflows: article image extraction, anti-bot-aware fetching, RSS/XML news monitoring, translation enrichment, deduplication, and Google Sheets delivery.

## Projects

| Project | What it automates | Highlights |
|---|---|---|
| [`article-image-extractor`](article-image-extractor/) | Finds and downloads content images from technical articles | Site-specific extractors, UI/ad/avatar filtering, Cloudflare-aware browser fallback, CLI and lightweight web UI |
| [`news-monitoring-pipeline`](news-monitoring-pipeline/) | Collects fresh smartphone-industry news and writes translated summaries to Google Sheets | RSS/XML sitemap ingestion, timezone-safe deduplication, batch Sheets API writes, cron-friendly execution |

## Operational Context

Editorial teams often spend time on repetitive content operations: opening source articles, collecting illustrations, checking news feeds, translating summaries, and copying data into shared spreadsheets. These projects convert those workflows into repeatable pipelines with clear failure modes and minimal manual steps.

The engineering focus is not just scraping pages. The useful part is operational reliability:

- stable sources first: RSS and XML sitemaps before fragile HTML scraping;
- state stored where the team already works: Google Sheets as the deduplication source of truth;
- controlled network behavior: sequential queues and browser fallback only when needed;
- regression safety: deterministic helper tests and characterization tests around filtering logic;
- maintainability: explicit entry points, focused modules, and documentation of operational assumptions.

## Repository Layout

```text
editorial-automation-toolkit/
├── article-image-extractor/
│   ├── download_images.py
│   ├── site_extractors.py
│   ├── cf_browser_fetch.py
│   ├── web_app.py
│   └── tests/
├── news-monitoring-pipeline/
│   ├── news_scraper.py
│   └── requirements.txt
└── README.md
```

## Security Notes

Runtime credentials and local state are intentionally excluded:

- Google service-account keys;
- `.env` files;
- cron logs;
- downloaded images and generated archives;
- local tool configuration and machine-specific state.

Only implementation files and non-sensitive documentation are kept in version control.
