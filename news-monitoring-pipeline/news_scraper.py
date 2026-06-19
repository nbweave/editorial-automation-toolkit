"""
news_scraper.py — Парсер новостей GSMArena и PhoneArena → Google Sheets.

НАЗНАЧЕНИЕ
    Собирает свежие новости из двух источников, переводит аннотации EN→RU
    и аккуратно дописывает их в Google-таблицу. Дедупликация — по уже
    лежащим в таблице ссылкам + по "cutoff" (самая свежая дата каждого
    домена в таблице).

ИСТОЧНИКИ
    - GSMArena   : RSS-фид (https://www.gsmarena.com/rss-news-reviews.php3).
                   ~20 свежих новостей с заголовками и pubDate.
                   og:description тянется отдельным запросом со страницы.
    - PhoneArena : месячный sitemap (sitemaps/news/{year}/{month:02d}/index.xml)
                   и Google News sitemap (sitemaps/googlenews.xml).
                   Sitemap'ы не защищены Cloudflare — поэтому используем их,
                   а не парсинг HTML.

КУДА ПИШЕТ
    Google Sheets, таблица SPREADSHEET_ID или SPREADSHEET_NAME,
    лист WORKSHEET_NAME.
    Столбцы: A=Статус[НЕ ТРОГАТЬ — ведётся вручную], B=Дата, C=Время,
             D=Аннотация(RU), E=Ссылка.

РАСПИСАНИЕ
    Production pattern: cron/systemd timer каждые несколько минут.
    Лог пишется в cron.log (формат строк log.info стабилен — не менять).

ВЕРСИЯ / ДАТА
    v5.0 — актуально на 2026-05.

ЗАВИСИМОСТИ
    requests, beautifulsoup4 (+lxml для XML), gspread, google-auth, deep_translator.

ЧУВСТВИТЕЛЬНЫЕ ФАЙЛЫ
    credentials.json — service-account ключ Google. НЕ коммитить, НЕ трогать руками.
"""

import re
import logging
import os
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

import requests
from bs4 import BeautifulSoup
import gspread
from google.oauth2.service_account import Credentials
from deep_translator import GoogleTranslator


# --- CONFIG ---

SERVICE_ACCOUNT_FILE = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "credentials.json")

SPREADSHEET_NAME = os.environ.get("NEWS_SCRAPER_SPREADSHEET_NAME", "")
SPREADSHEET_ID = os.environ.get("NEWS_SCRAPER_SPREADSHEET_ID", "")

WORKSHEET_NAME = os.environ.get("NEWS_SCRAPER_WORKSHEET_NAME", "News")

# Московское время = UTC+3 (без DST, постоянный сдвиг)
MSK = timezone(timedelta(hours=3))

# Столбцы: A=Статус[не трогаем, заполняется вручную], B=Дата, C=Время,
#          D=Аннотация, E=Ссылка
COL_LINK = 5  # E — по этому столбцу определяем первую пустую строку

# Источники
GSMARENA_RSS = "https://www.gsmarena.com/rss-news-reviews.php3"
PHONEARENA_MONTH_SITEMAP = "https://www.phonearena.com/sitemaps/news/{year}/{month:02d}/index.xml"
PHONEARENA_GOOGLENEWS = "https://www.phonearena.com/sitemaps/googlenews.xml"

# Единый User-Agent для всех HTTP-запросов (некоторые источники режут
# дефолтный python-requests/UA).
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


# --- TRANSLATION ---

translator = GoogleTranslator(source="en", target="ru")


def translate_text(text: str) -> str:
    """Переводит строку EN→RU через GoogleTranslator.

    При любой ошибке переводчика возвращает исходный текст (fail-safe:
    лучше записать английский, чем уронить весь запуск из-за сетевой
    ошибки в одной статье).

    Args:
        text: исходный текст на английском (может быть пустым).

    Returns:
        Переведённая строка либо исходный текст при ошибке.
        Пустая строка, если на вход подан falsy-аргумент.
    """
    if not text:
        return ""
    try:
        return translator.translate(text)
    except Exception as e:
        log.warning(f"  Ошибка перевода: {e}")
        return text


# --- LOGGING ---

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# --- HELPERS ---

def title_from_url(url: str) -> str:
    """Извлекает читаемый заголовок из URL-слага PhoneArena.

    Формат URL: ``.../news/Some-headline-words_id12345``.
    Берём часть после ``/news/``, отбрасываем суффикс ``_idNNNN``,
    меняем дефисы на пробелы и капитализируем первую букву.

    Args:
        url: полная ссылка на статью PhoneArena.

    Returns:
        Человекочитаемый заголовок либо пустая строка, если URL не
        соответствует ожидаемому формату.
    """
    match = re.search(r"/news/(.+?)(?:_id\d+)?$", url)
    if not match:
        return ""
    slug = match.group(1)
    title = slug.replace("-", " ").strip()
    return title[0].upper() + title[1:] if title else ""


def fetch_og_description(url: str) -> str:
    """Загружает og:description со страницы статьи GSMArena.

    Делает один HTTP GET с UA-заголовком и таймаутом 20с, ищет тег
    ``<meta property="og:description" content="...">``.

    Args:
        url: полная ссылка на статью GSMArena.

    Returns:
        Содержимое og:description (trim) либо пустая строка, если тег
        не найден или запрос упал.
    """
    try:
        resp = requests.get(url, headers={"User-Agent": UA}, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        og = soup.find("meta", property="og:description")
        if og and og.get("content"):
            return og["content"].strip()
    except Exception as e:
        log.warning(f"  Не удалось загрузить описание: {e}")
    return ""


# --- GSMArena RSS parser ---

def parse_gsmarena_rss() -> list[dict]:
    """Парсит RSS-фид GSMArena и возвращает список статей.

    Один HTTP-запрос на список. Описания (og:description) здесь не
    берём — это слишком дорого для всех 20 статей; их тянем отдельно
    в main() только для тех, что прошли cutoff.

    Returns:
        Список словарей со полями: ``url``, ``source`` ("GSMArena"),
        ``title``, и (опционально, если pubDate распарсился) ``date``
        (DD.MM.YYYY), ``time`` (HH:MM), ``dt`` (datetime с tz=MSK).
        Пустой список при ошибке сети.
    """
    try:
        resp = requests.get(GSMARENA_RSS, headers={"User-Agent": UA}, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        log.error(f"Ошибка загрузки RSS GSMArena: {e}")
        return []

    soup = BeautifulSoup(resp.text, "xml")
    articles: list[dict] = []

    for item in soup.find_all("item"):
        link = item.find("link")
        title = item.find("title")
        pub_date = item.find("pubDate")

        if not link or not link.text:
            continue

        url = link.text.strip()

        # Фильтр: в RSS попадают обзоры, рейтинги и пр. — нам нужны только
        # новости. У них в URL есть подстрока "-news-".
        if "-news-" not in url:
            continue

        art: dict = {
            "url": url,
            "source": "GSMArena",
            "title": title.text.strip() if title else "",
        }

        # pubDate в RFC-2822: "Mon, 16 Mar 2026 16:03:02 +0100" — приводим к МСК.
        if pub_date and pub_date.text:
            try:
                dt = parsedate_to_datetime(pub_date.text.strip())
                dt_msk = dt.astimezone(MSK)
                art["date"] = dt_msk.strftime("%d.%m.%Y")
                art["time"] = dt_msk.strftime("%H:%M")
                art["dt"] = dt_msk
            except (ValueError, TypeError):
                pass

        articles.append(art)

    return articles


# --- PhoneArena sitemap parser ---

def parse_phonearena_month(year: int, month: int) -> list[dict]:
    """Парсит месячный sitemap PhoneArena за заданный год/месяц.

    Все статьи за месяц возвращаются разом — sitemap содержит lastmod
    (используем как дату публикации в МСК).

    Args:
        year: 4-значный год.
        month: номер месяца 1–12.

    Returns:
        Список словарей: ``url``, ``source`` ("PhoneArena"), ``title``
        (из URL-слага — потом перезаписывается из Google News sitemap)
        и (если lastmod распарсился) ``date``/``time``/``dt``.
        Пустой список при ошибке сети.
    """
    url = PHONEARENA_MONTH_SITEMAP.format(year=year, month=month)

    try:
        resp = requests.get(url, headers={"User-Agent": UA}, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        log.error(f"Ошибка загрузки {url}: {e}")
        return []

    soup = BeautifulSoup(resp.text, "xml")
    articles: list[dict] = []

    for url_tag in soup.find_all("url"):
        loc = url_tag.find("loc")
        if not loc:
            continue
        href = loc.text.strip()
        if "/news/" not in href:
            continue

        lastmod = url_tag.find("lastmod")
        dt_msk: datetime | None = None
        if lastmod and lastmod.text:
            try:
                dt = datetime.fromisoformat(lastmod.text.strip())
                dt_msk = dt.astimezone(MSK)
            except ValueError:
                pass

        art: dict = {
            "url": href,
            "source": "PhoneArena",
            "title": title_from_url(href),
        }
        if dt_msk:
            art["date"] = dt_msk.strftime("%d.%m.%Y")
            art["time"] = dt_msk.strftime("%H:%M")
            art["dt"] = dt_msk

        articles.append(art)

    return articles


def fetch_googlenews_titles() -> dict[str, str]:
    """Загружает заголовки статей из Google News sitemap PhoneArena.

    В Google News sitemap (~30 самых свежих статей) лежат настоящие
    заголовки в теге ``<title>`` — намного лучше, чем сконвертированный
    из URL-слага вариант. Используем для тех PhoneArena-статей, что
    есть и в месячном, и в Google News sitemap'ах.

    Returns:
        Словарь ``{url: title}``. Пустой словарь при ошибке сети.
    """
    titles: dict[str, str] = {}
    try:
        resp = requests.get(PHONEARENA_GOOGLENEWS, headers={"User-Agent": UA}, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "xml")
        for url_tag in soup.find_all("url"):
            loc = url_tag.find("loc")
            title_tag = url_tag.find("title")
            if loc and title_tag:
                titles[loc.text.strip()] = title_tag.text.strip()
    except Exception as e:
        log.warning(f"Не удалось загрузить Google News sitemap: {e}")
    return titles


# --- Google Sheets connection ---

def connect_google_sheets() -> gspread.Worksheet:
    """Подключается к Google Sheets и возвращает worksheet.

    Авторизация через service-account credentials.json со scope'ами
    spreadsheets + drive. Если задан SPREADSHEET_ID — открываем по нему,
    иначе по имени SPREADSHEET_NAME.

    Returns:
        Открытый ``gspread.Worksheet`` (лист WORKSHEET_NAME).

    Raises:
        ValueError: если не задано ни SPREADSHEET_ID, ни SPREADSHEET_NAME.
        FileNotFoundError: если credentials.json отсутствует.
        gspread.exceptions.SpreadsheetNotFound: таблица не найдена / нет доступа.
        gspread.exceptions.WorksheetNotFound: лист не найден.
    """
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=scopes)
    gc = gspread.authorize(creds)

    if SPREADSHEET_ID:
        sh = gc.open_by_key(SPREADSHEET_ID)
    elif SPREADSHEET_NAME:
        sh = gc.open(SPREADSHEET_NAME)
    else:
        raise ValueError("Укажи SPREADSHEET_NAME или SPREADSHEET_ID!")

    return sh.worksheet(WORKSHEET_NAME)


# --- Google Sheets read helpers ---

def get_existing_data(ws: gspread.Worksheet) -> tuple[set, dict[str, datetime]]:
    """Считывает существующие ссылки и cutoff-даты из таблицы.

    Один batch-запрос на диапазон B:E. Для каждой строки:
      - URL (столбец E) добавляется в множество для O(1) проверки дубля;
      - дата+время (столбцы B, C) парсятся; для каждого домена
        запоминаем самую свежую — это cutoff для отсечения старых
        статей при следующем парсинге.

    Args:
        ws: рабочий лист Google Sheets.

    Returns:
        Кортеж (existing_urls, cutoffs):
          - existing_urls: ``set[str]`` всех URL из столбца E.
          - cutoffs: ``dict[str, datetime]`` — ключ "gsmarena" или
            "phonearena", значение — самая свежая дата (tz=MSK).
    """
    # Один batch-запрос: B (дата), C (время), D (аннотация), E (ссылка).
    all_data = ws.get("B:E")

    existing_urls: set = set()
    cutoffs: dict[str, datetime] = {}  # "gsmarena" -> dt, "phonearena" -> dt

    for row in all_data[1:]:  # пропускаем строку заголовков
        if len(row) < 4 or not row[3]:
            continue

        url = row[3]
        existing_urls.add(url)

        date_str = row[0] if len(row) > 0 else ""
        time_str = row[1] if len(row) > 1 else ""

        if not date_str or not time_str:
            continue

        # Определяем домен по подстроке в URL — этого достаточно, т.к.
        # у нас всего два источника.
        domain: str | None = None
        if "gsmarena.com" in url:
            domain = "gsmarena"
        elif "phonearena.com" in url:
            domain = "phonearena"
        else:
            continue

        try:
            dt = datetime.strptime(f"{date_str} {time_str}", "%d.%m.%Y %H:%M")
            dt = dt.replace(tzinfo=MSK)
            if domain not in cutoffs or dt > cutoffs[domain]:
                cutoffs[domain] = dt
        except ValueError:
            continue

    return existing_urls, cutoffs


def get_first_empty_row(ws: gspread.Worksheet) -> int:
    """Возвращает номер первой пустой строки по столбцу E (ссылка).

    Использует столбец E, а не A, потому что A — это "Статус" (ведётся
    вручную и может быть пустым у уже заполненных строк).

    Args:
        ws: рабочий лист Google Sheets.

    Returns:
        1-индексированный номер первой строки, в которую можно писать.
    """
    values = ws.col_values(COL_LINK)
    return len(values) + 1


# --- Google Sheets write helpers ---

def write_articles(ws: gspread.Worksheet, articles: list[dict], start_row: int) -> int:
    """Записывает статьи в таблицу в столбцы B–E.

    Столбец A (Статус) НЕ затрагивается. Один batch-update на все строки.

    Args:
        ws: рабочий лист Google Sheets.
        articles: список словарей со полями ``date``, ``time``,
            ``annotation``, ``url`` (последнее обязательно).
        start_row: с какой строки начинать запись (1-индексированно).

    Returns:
        Количество фактически записанных строк (0, если articles пуст).
    """
    if not articles:
        return 0

    rows: list[list[str]] = []
    for art in articles:
        rows.append([
            art.get("date", ""),
            art.get("time", ""),
            art.get("annotation", ""),
            art["url"],
        ])

    end_row = start_row + len(rows) - 1
    ws.update(
        values=rows,
        range_name=f"B{start_row}:E{end_row}",
        value_input_option="USER_ENTERED",
    )

    return len(rows)


# --- MAIN ---

def main() -> None:
    """Главная точка входа: оркестрация всего цикла парсинга.

    Шаги:
      1. Подключение к Google Sheets.
      2. Чтение существующих URL + cutoff-дат по доменам.
      3. Парсинг GSMArena RSS, фильтр по cutoff, подтягивание
         og:description, перевод.
      4. Парсинг месячного sitemap'а PhoneArena (и предыдущего
         месяца, если сейчас 1–3 число — статьи последних дней
         могут оказаться в прошлом месяце на стыке).
      5. Обогащение заголовков PhoneArena из Google News sitemap.
      6. Сортировка всех новых статей по dt (по возрастанию).
      7. Запись batch'ем в таблицу.

    Returns:
        None. Все ошибки логируются; падение на одном этапе не
        обязательно останавливает обработку другого источника
        (исключение — недоступность Google Sheets: тогда выходим).
    """
    log.info("=" * 50)
    log.info("Запуск парсера новостей v5.0")
    log.info("=" * 50)

    now = datetime.now(MSK)

    # ── Шаг 1: Google Sheets ──────────────────────────────────────
    try:
        ws = connect_google_sheets()
        log.info("Подключение к таблице — OK")
    except Exception as e:
        log.error(f"Не удалось подключиться к Google Sheets: {e}")
        return

    # ── Шаг 2: существующие данные ────────────────────────────────
    existing_urls, cutoffs = get_existing_data(ws)
    log.info(f"В таблице уже {len(existing_urls)} ссылок")
    for domain, dt in cutoffs.items():
        log.info(f"  Последняя {domain}: {dt.strftime('%d.%m.%Y %H:%M')}")

    all_new_articles: list[dict] = []

    # ── Шаг 3: GSMArena (RSS) ─────────────────────────────────────
    log.info("Парсим GSMArena (RSS)...")
    gsm_articles = parse_gsmarena_rss()
    log.info(f"  Статей в RSS: {len(gsm_articles)}")

    gsm_cutoff = cutoffs.get("gsmarena")
    new_gsm = [
        a for a in gsm_articles
        if a["url"] not in existing_urls
        and (not gsm_cutoff or a.get("dt") and a["dt"] > gsm_cutoff)
    ]
    log.info(f"  Новых (после cutoff): {len(new_gsm)}")

    for art in new_gsm:
        # og:description со страницы статьи (1 запрос на статью) —
        # делаем только для НОВЫХ, иначе будет 20 лишних запросов на каждый cron-тик.
        desc = fetch_og_description(art["url"])
        art["annotation"] = translate_text(desc) if desc else translate_text(art["title"])
        log.info(f"    {art.get('date', '???')} {art.get('time', '???')} — {art['title'][:50]}")

    all_new_articles.extend(new_gsm)

    # ── Шаг 4: PhoneArena (sitemap) ───────────────────────────────
    log.info("Парсим PhoneArena (месячный sitemap)...")

    all_pa = parse_phonearena_month(now.year, now.month)

    # На стыке месяцев (1–3 число) свежие статьи могут ещё лежать в
    # прошлом месяце — поэтому подгружаем и его.
    if now.day <= 3:
        prev = now.replace(day=1) - timedelta(days=1)
        log.info(f"  Также проверяем {prev.year}-{prev.month:02d}...")
        all_pa.extend(parse_phonearena_month(prev.year, prev.month))

    log.info(f"  Статей в sitemap: {len(all_pa)}")

    pa_cutoff = cutoffs.get("phonearena")
    new_pa = [
        a for a in all_pa
        if a["url"] not in existing_urls
        and (not pa_cutoff or a.get("dt") and a["dt"] > pa_cutoff)
    ]
    log.info(f"  Новых (после cutoff): {len(new_pa)}")

    if new_pa:
        # Google News sitemap нужен только если действительно есть новые
        # PhoneArena-статьи: иначе тратить запрос смысла нет.
        gn_titles = fetch_googlenews_titles()
        log.info(f"  Заголовков из Google News: {len(gn_titles)}")

        for art in new_pa:
            if art["url"] in gn_titles:
                art["title"] = gn_titles[art["url"]]
            art["annotation"] = translate_text(art["title"])
            log.info(f"    {art.get('date', '???')} {art.get('time', '???')} — {art['title'][:50]}")

    all_new_articles.extend(new_pa)

    # ── Шаг 6: сортировка ─────────────────────────────────────────
    # far_future — fallback-ключ сортировки для статей без даты (`dt`):
    # такие уйдут в конец списка, в таблицу попадут после датированных.
    far_future = datetime.max.replace(tzinfo=MSK)
    all_new_articles.sort(key=lambda a: a.get("dt", far_future))

    if all_new_articles:
        log.info(f"Итого {len(all_new_articles)} новых статей:")
        for art in all_new_articles[:10]:
            log.info(f"  {art.get('date', '???')} {art.get('time', '???')} [{art['source']}] {art['title'][:40]}")
        if len(all_new_articles) > 10:
            log.info(f"  ... и ещё {len(all_new_articles) - 10}")

    # ── Шаг 7: запись ─────────────────────────────────────────────
    total_added = 0
    if all_new_articles:
        start_row = get_first_empty_row(ws)
        added = write_articles(ws, all_new_articles, start_row)
        total_added = added
        log.info(f"Записано {added} строк, начиная с {start_row}")

    log.info("=" * 50)
    log.info(f"Готово! Добавлено строк: {total_added}")
    log.info("=" * 50)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Остановлено пользователем.")
