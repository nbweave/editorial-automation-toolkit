# Article Image Extractor

Automation tool for extracting content images from web articles while filtering out ads, avatars, logos, UI icons, tracking assets, and recommendation blocks.

It was built for an editorial workflow where manual image collection from source articles was slow and error-prone.

## What It Does

- accepts article URLs from `urls.txt` or from the command line;
- extracts likely content images from article HTML;
- handles site-specific structures for sources such as GSMArena, PhoneArena, ZDNet, Tom's Hardware, TechRadar, and generic articles;
- filters out non-content assets using URL patterns, DOM context, dimensions, alt text, and source-specific rules;
- falls back to a real Chromium browser when Cloudflare/Turnstile blocks direct HTML fetching;
- saves images into per-article folders;
- optionally serves a small password-protected web UI for non-technical teammates.

## Quick Start

```bash
python -m pip install -r requirements.txt
python download_images.py https://example.com/article
```

Batch mode:

```bash
printf '%s\n' 'https://example.com/article-1' 'https://example.com/article-2' > urls.txt
python download_images.py --urls-file urls.txt
```

Run tests:

```bash
python -m pytest
```

## Main Files

| File | Purpose |
|---|---|
| `download_images.py` | Main CLI and image filtering logic |
| `site_extractors.py` | Site-specific article image extractors |
| `cf_browser_fetch.py` | Chromium fallback for Cloudflare-protected pages |
| `web_app.py` | Lightweight stdlib web UI with queue and zip output |
| `tests/` | Unit and characterization tests for filtering behavior |

## Cloudflare-Aware Fetching

The pipeline first tries direct HTTP fetching for speed. If a page returns an anti-bot challenge, `cf_browser_fetch.py` can launch Chromium and return the rendered HTML to the extractor. Image downloading still uses the faster HTTP path when possible.

This keeps the expensive browser step scoped to the part that needs it.

## Web UI

The web UI is intentionally dependency-light and uses only the Python standard library for HTTP serving. Runtime settings are controlled by environment variables:

| Variable | Default | Purpose |
|---|---:|---|
| `IMG_WEB_PASSWORD` | empty | Required password for login |
| `IMG_WEB_HOST` | `0.0.0.0` | Bind host |
| `IMG_WEB_PORT` | `8787` | Bind port |
| `IMG_WEB_MAX_URLS` | `30` | Max URLs per job |
| `IMG_WEB_JOB_TTL` | `21600` | Job archive retention in seconds |

Example:

```bash
IMG_WEB_PASSWORD='change-me' python web_app.py
```

## Design Notes

The extractor is built around source-specific parsing, defensive filtering, anti-bot fallback, batch operations, a small internal UI, and test coverage around behavior that is easy to break during refactoring.
