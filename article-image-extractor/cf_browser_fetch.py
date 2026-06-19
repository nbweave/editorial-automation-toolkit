#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cloudflare-challenge fetcher (worker process).

Запускается основным скриптом ТОЛЬКО когда обычный HTTP-запрос (curl_cffi)
упёрся в антибот-проверку Cloudflare ("Just a moment...").

Открывает страницу настоящим браузером (nodriver/Chromium), дожидается, пока
Cloudflare-проверка решится, прокручивает страницу для подгрузки ленивых
картинок и сохраняет готовый HTML в файл.

Использование:
    python cf_browser_fetch.py <URL> <OUTPUT_HTML_FILE> [--timeout 75]

Код возврата: 0 — успех (HTML записан), 1 — не удалось решить проверку, 2 — ошибка.

На Linux без графической среды запускать через xvfb-run (это делает основной
скрипт автоматически). На macOS/Windows работает напрямую.
"""
import asyncio
import glob
import os
import platform
import sys
import time

# ВНИМАНИЕ: поддерживать синхронно с ArticleImageDownloader.CHALLENGE_MARKERS
# в download_images.py. Там есть дополнительный маркер "cf-mitigated" — это
# НАМЕРЕННО: основной скрипт сканирует тело HTTP-ответа (где служебная строка
# cf-mitigated встречается), а здесь проверяется только заголовок страницы.
CHALLENGE_MARKERS = ("just a moment", "verifying you are human", "checking your browser")


def find_chrome() -> str:
    """Найти исполняемый файл Chrome/Chromium на любой платформе."""
    env = os.environ.get("CF_FETCH_CHROME")
    if env and os.path.exists(env):
        return env

    home = os.path.expanduser("~")
    patterns = [
        # Playwright-браузеры (Linux/mac)
        f"{home}/.cache/ms-playwright/chromium-*/chrome-linux*/chrome",
        f"{home}/.cache/ms-playwright/chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium",
        f"{home}/Library/Caches/ms-playwright/chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium",
    ]
    found = []
    for pat in patterns:
        found.extend(glob.glob(pat))
    if found:
        # самая свежая версия (по номеру в пути)
        found.sort()
        return found[-1]

    # системные пути
    candidates = [
        "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium", "/usr/bin/chromium-browser",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return ""  # пусть nodriver сам поищет


async def get_title(page) -> str:
    try:
        return str(await page.evaluate("document.title"))
    except Exception:
        return ""


def is_challenge_html(text: str) -> bool:
    low = text.lower()
    return any(m in low for m in CHALLENGE_MARKERS)


async def fetch(url: str, timeout: float) -> str:
    import nodriver as uc

    chrome = find_chrome()
    start_kwargs = dict(
        headless=False,                       # headless НЕ проходит CF-проверку
        no_sandbox=True,
        browser_args=["--no-sandbox", "--disable-dev-shm-usage",
                      "--disable-blink-features=AutomationControlled"],
    )
    if chrome:
        start_kwargs["browser_executable_path"] = chrome

    browser = await uc.start(**start_kwargs)
    try:
        page = await browser.get(url)
        deadline = time.time() + timeout
        solved = False
        while time.time() < deadline:
            await asyncio.sleep(3)
            title = await get_title(page)
            if title and not is_challenge_html(title):
                solved = True
                break
        if not solved:
            return ""  # не решилось

        # прокрутка для подгрузки ленивых изображений
        for _ in range(12):
            try:
                await page.evaluate(
                    "window.scrollBy(0, Math.max(500, document.body.scrollHeight/12))")
            except Exception:
                pass
            await asyncio.sleep(0.4)
        await asyncio.sleep(1.5)

        html = await page.get_content()   # последний вызов через CDP (после него evaluate нестабилен)
        if html and not is_challenge_html(html[:5000]):
            return html
        return html or ""
    finally:
        try:
            browser.stop()
        except Exception:
            pass


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: cf_browser_fetch.py <URL> <OUTPUT_FILE> [--timeout N]", file=sys.stderr)
        return 2
    url = sys.argv[1]
    out_file = sys.argv[2]
    timeout = 75.0
    if "--timeout" in sys.argv:
        try:
            timeout = float(sys.argv[sys.argv.index("--timeout") + 1])
        except (ValueError, IndexError):
            pass

    try:
        html = asyncio.run(fetch(url, timeout))
    except Exception as e:
        print(f"cf_browser_fetch error: {type(e).__name__}: {e}", file=sys.stderr)
        return 2

    if not html or is_challenge_html(html[:5000]):
        print("cf_browser_fetch: challenge not solved", file=sys.stderr)
        return 1

    with open(out_file, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"cf_browser_fetch: OK ({len(html)} bytes) -> {out_file}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
