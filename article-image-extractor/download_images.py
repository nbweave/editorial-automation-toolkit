#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Article Image Downloader (v2.4 - curl_cffi edition).
- Uses curl_cffi to bypass Cloudflare and other bot protection
- Based on v2.3 with all original features preserved
"""

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
from html import unescape
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode, unquote

from curl_cffi import requests
from curl_cffi.requests import exceptions as curl_exceptions
from bs4 import BeautifulSoup
from PIL import Image

from site_extractors import (
    GSMArenaExtractor,
    JuxtaposeExtractor,
    PhoneArenaExtractor,
    TomsHardwarePaginator,
    ZDNetExtractor,
)

if sys.version_info < (3, 8):
    raise RuntimeError("Python 3.8+ is required to run this script.")


class ArticleImageDownloader:
    """
    Загрузчик изображений из веб-статей с умной фильтрацией.

    Возможности:
    - Поиск контентных изображений (игнорирует рекламу, аватары, UI-иконки)
    - Обложка (OG/Twitter и из DOM), галереи (JSON/PhoneArena), Juxtapose
    - Полноразмерные GSM Arena и поддержка пагинации Tom's Hardware
    - Валидация изображений и ограничение размера
    - Обход Cloudflare через curl_cffi с impersonate
    """

    # Константы по умолчанию (могут переопределяться из __init__/CLI)
    MAX_FILENAME_LENGTH = 80
    DEFAULT_CHUNK_SIZE = 8192
    MAX_FILE_SIZE_MB = 50     # Максимальный размер файла в МБ
    MAX_PAGES = 20            # Максимальное количество страниц для многостраничных статей
    ALLOWED_SCHEMES = ['http', 'https']

    # ---- Правила фильтра is_ui_element (порядок применения см. в методе) ----
    # Спасающий whitelist по alt — перекрывает любой blacklist ниже
    UI_CONTENT_ALT_WHITELIST = (
        'review', 'phone', 'device', 'smartphone', 'laptop',
        'side button', 'volume', 'power button', 'physical',
        'product', 'gadget', 'hardware',
    )
    # Blacklist по URL (подстрока, lower-case)
    UI_URL_BLACKLIST = (
        '/button/', '/btn/', '/icon/', '/logo/', '/badge/',
        'button-', '-btn-', 'icon-', 'logo-',
        'google-news', 'follow', 'subscribe',
        'social-', 'share-', 'arrow-', 'chevron',
        'spinner', 'loader', 'placeholder',
        'bg-', 'watermark',
    )
    # Blacklist по alt (подстрока, lower-case); 'newsletter' — бывшая отдельная
    # ветка, влита сюда (результат тот же True, whitelist проверяется раньше)
    UI_ALT_BLACKLIST = (
        'follow us', 'subscribe', 'share button',
        'click here', 'download button', 'menu icon',
        'close button', 'next arrow', 'previous arrow',
        'newsletter',
    )

    def __init__(
        self,
        download_dir: str = "downloaded_images",
        min_size: int = 20,
        debug: bool = True,
        pause_between_downloads: float = 0.5,
        max_file_size_mb: Optional[int] = None,
        max_pages: Optional[int] = None,
        hash_dedup: bool = False,
        log_file: Optional[str] = None,
        browser_fallback: str = "auto",
        browser_timeout: float = 75.0,
        dry_run: bool = False,
    ):
        self.download_dir = download_dir
        self.min_size = min_size
        self.debug = debug
        self.pause_between_downloads = pause_between_downloads
        self.hash_dedup = hash_dedup
        self._seen_hashes: Set[str] = set()

        # Обход Cloudflare-проверки через настоящий браузер:
        # 'auto'   — пробовать браузер только когда обычный запрос упёрся в проверку
        # 'always' — всегда брать HTML через браузер
        # 'never'  — никогда (старое поведение)
        self.browser_fallback = browser_fallback
        self.browser_timeout = browser_timeout

        # Режим --dry-run: пройти весь пайплайн поиска картинок,
        # но ничего не скачивать и не создавать папок —
        # только напечатать нормализованные URL (для регресс-проверок).
        self.dry_run = dry_run

        # Переопределяем лимиты при необходимости
        if max_file_size_mb is not None:
            self.MAX_FILE_SIZE_MB = max_file_size_mb
        if max_pages is not None:
            self.MAX_PAGES = max_pages

        # Логирование
        self._setup_logging(log_file)

        # HTTP-сессия с curl_cffi (impersonate Chrome для обхода Cloudflare)
        self.session = requests.Session(impersonate="chrome")

        # Сайт-специфичные экстракторы (см. site_extractors.py; порядок их
        # вызова в find_content_images критичен и задокументирован там же)
        self.phonearena = PhoneArenaExtractor(self)
        self.juxtapose = JuxtaposeExtractor(self)
        self.zdnet = ZDNetExtractor(self)
        self.gsmarena = GSMArenaExtractor(self)
        self.tomshardware = TomsHardwarePaginator(self)

        # Папка для загрузки
        os.makedirs(download_dir, exist_ok=True)
        self._folder_counter = self._get_next_folder_number()

    # ---------- ЛОГИ ----------
    def _setup_logging(self, log_file: Optional[str] = None) -> None:
        level = logging.DEBUG if self.debug else logging.INFO
        self.logger = logging.getLogger("article_image_downloader")
        self.logger.setLevel(level)

        # Сбрасываем предыдущие хендлеры, чтобы избежать дублирования
        if self.logger.handlers:
            for h in list(self.logger.handlers):
                self.logger.removeHandler(h)

        fmt = logging.Formatter('%(levelname)s: %(message)s')

        sh = logging.StreamHandler()
        sh.setLevel(level)
        sh.setFormatter(fmt)
        self.logger.addHandler(sh)

        if log_file:
            fh = logging.FileHandler(log_file, encoding='utf-8')
            fh.setLevel(level)
            fh.setFormatter(fmt)
            self.logger.addHandler(fh)

    # ---------- ПАПКИ ----------
    def _get_existing_dirs(self) -> List[str]:
        try:
            return [
                d for d in os.listdir(self.download_dir)
                if os.path.isdir(os.path.join(self.download_dir, d))
            ]
        except FileNotFoundError:
            os.makedirs(self.download_dir, exist_ok=True)
            return []

    def _extract_folder_number(self, dirname: str) -> Optional[int]:
        m = re.match(r'^(\d+)\.\s?', dirname)
        return int(m.group(1)) if m else None

    def _get_next_folder_number(self) -> int:
        dirs = self._get_existing_dirs()
        numbers = [num for d in dirs if (num := self._extract_folder_number(d)) is not None]
        return max(numbers) if numbers else 0

    def create_numbered_article_dir(self, title: str) -> str:
        safe_title = self.clean_filename(title) if title else 'article'
        n = self._folder_counter + 1
        while True:
            folder_name = f"{n}. {safe_title}"
            path = os.path.join(self.download_dir, folder_name)
            if not os.path.exists(path):
                os.makedirs(path, exist_ok=False)
                self._folder_counter = n
                self.logger.info(f"[OK] Создана папка статьи: {folder_name}")
                return path
            n += 1

    # ---------- ИМЕНА/ВАЛИДАЦИЯ ----------
    def clean_filename(self, filename: str) -> str:
        filename = re.sub(r'[<>:"/\\|?*]', '_', filename)
        filename = filename.replace("'", "").replace('"', "")
        filename = filename.replace('–', '-').replace('—', '-').replace('…', '...')
        filename = filename.strip(' .')
        filename = re.sub(r'\s+', ' ', filename)
        return filename[:self.MAX_FILENAME_LENGTH]

    def validate_url(self, url: str) -> bool:
        try:
            parsed = urlparse(url)
            if parsed.scheme not in self.ALLOWED_SCHEMES:
                self.logger.warning(f"[WARN] Небезопасная схема URL: {parsed.scheme}")
                return False
            return True
        except Exception as e:
            self.logger.error(f"[ERROR] Ошибка валидации URL: {e}")
            return False

    def _strip_query_fragment(self, url: str) -> str:
        parsed = urlparse(url)
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, '', '', ''))

    # ---------- ПАГИНАЦИЯ ----------
    def _collect_article_pages(self, soup: BeautifulSoup, base_url: str) -> List[str]:
        pages = {self._strip_query_fragment(base_url)}
        try:
            base_parsed = urlparse(base_url)
            base_path = base_parsed.path
            if self.tomshardware.matches(base_parsed.netloc):
                pages.update(self.tomshardware.collect_pages(soup, base_url))
                return sorted(pages)
            if '.php' in base_path:
                prefix = base_path.split('.php')[0]
                pattern = re.compile(re.escape(prefix) + r'p\d+\.php')
            else:
                prefix = base_path.rstrip('/')
                pattern = re.compile(re.escape(prefix) + r'/page/\d+')
            for link in soup.select('a[href]'):
                href = link['href']
                if not href:
                    continue
                absolute = urljoin(base_url, href)
                absolute_clean = self._strip_query_fragment(absolute)
                parsed = urlparse(absolute_clean)
                if parsed.netloc != base_parsed.netloc:
                    continue
                path = parsed.path
                if path == base_path:
                    pages.add(absolute_clean)
                else:
                    if pattern.match(path):
                        pages.add(absolute_clean)
        except Exception:
            pass
        return sorted(pages)

    # ---------- URL НОРМАЛИЗАЦИЯ/ДЕДУП ----------
    def _normalize_url(self, url: str) -> str:
        if not url:
            return ""
        url = url.strip()
        url = url.split('#', 1)[0]
        parsed = urlparse(url)
        path = re.sub(r'/+', '/', parsed.path)

        size_keys = {'w', 'h', 'width', 'height', 'size', 'resize', 'quality', 'format', 'webp', 'jpeg', 'crop'}
        query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
        filtered_pairs = [(k, v) for k, v in query_pairs if k.lower() not in size_keys]
        query = urlencode(filtered_pairs, doseq=True)

        normalized = urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, '', query, ''))
        normalized = normalized.rstrip('?&')
        return normalized

    def _looks_like_image_url(self, url: str) -> bool:
        if not url:
            return False
        sanitized = url.split('#', 1)[0].split('?', 1)[0].lower()
        image_exts = ('.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp', '.avif', '.heic', '.heif')
        return sanitized.endswith(image_exts)

    def _allow_duplicate_image(self, page_host: str, element, source: Optional[str]) -> bool:
        if source in {'gallery_item', 'hero'}:
            return False
        if not element:
            return False

        block_keywords = {
            'widget', 'promo', 'related', 'recommend', 'trending', 'store', 'shop', 'merchant',
            'logo', 'sponsored', 'newsletter', 'footer', 'header', 'sidebar', 'ads', 'advert'
        }
        inline_keywords = {
            'inline', 'bodycopy', 'article', 'content', 'text', 'post', 'entry', 'review', 'main'
        }

        inline_candidate = False
        ancestor = element
        depth = 0
        while ancestor is not None and depth < 6:
            classes = ancestor.get('class') or []
            if isinstance(classes, str):
                classes = [classes]
            class_str = ' '.join(classes).lower()
            if class_str:
                if any(keyword in class_str for keyword in block_keywords):
                    return False
                if any(keyword in class_str for keyword in inline_keywords):
                    inline_candidate = True
            ancestor = getattr(ancestor, 'parent', None)
            depth += 1

        figure = element if getattr(element, 'name', None) == 'figure' else element.find_parent('figure')
        if figure:
            classes = figure.get('class') or []
            if isinstance(classes, str):
                classes = [classes]
            class_str = ' '.join(classes).lower()
            if any(keyword in class_str for keyword in inline_keywords):
                inline_candidate = True

        return inline_candidate

    def _extract_image_id(self, url: str) -> Optional[str]:
        if not url:
            return None
        m = re.search(r'/([0-9]+)-(?:image|[0-9]+)(?:/|$)', url)
        if m:
            return m.group(1)
        m = re.search(r'/([0-9]+)[\-_][^/]+\.(?:jpg|jpeg|png|webp)$', url)
        if m:
            return m.group(1)
        return None

    def _extract_image_stub(self, url: str) -> Optional[str]:
        if not url:
            return None
        parsed = urlparse(url)
        filename = os.path.basename(parsed.path)
        if not filename:
            return None
        stub = os.path.splitext(filename)[0].lower()
        stub = re.sub(r'-(?:[0-9]{3,}-[0-9]{2,}|[0-9]{3,}x[0-9]{2,})$', '', stub)
        return self._apply_domain_stub_rules(parsed, stub)

    def _apply_domain_stub_rules(self, parsed_url, stub: Optional[str]) -> Optional[str]:
        if not stub:
            return stub
        host = parsed_url.netloc.lower()
        if self.gsmarena.matches(host):
            return self.gsmarena.augment_stub(parsed_url, stub)
        return stub

    def _should_skip_by_url_pattern(self, img_url: str, page_url: str, element=None) -> Optional[str]:
        if not img_url:
            return "Пустой URL"

        url_lower = img_url.lower()
        page_lower = page_url.lower()
        parsed_url = urlparse(img_url)
        host_lower = parsed_url.netloc.lower()
        path_lower = parsed_url.path.lower()

        ad_patterns = [
            '/announcements/', '/announcement/', '/ads/', '/ad/', '/banner', '/promo', 'affiliate',
            '/static/stores/', '/vv/bigpic/', 'arenaev.com'
        ]
        for pattern in ad_patterns:
            if pattern in url_lower:
                if pattern == '/vv/bigpic/' and element is not None:
                    parent = element.find_parent('p')
                    if parent:
                        parent_classes = parent.get('class') or []
                        if isinstance(parent_classes, str):
                            parent_classes = [parent_classes]
                        if 'image-row' in parent_classes:
                            return None
                return f"Реклама/баннер ({pattern})"

        recommendation_patterns = [
            '/recommended/', '/related/', '/trending/', '/popular/', '/latest/', '/news/', '/article/', '/topics/'
        ]
        if any(pattern in path_lower for pattern in recommendation_patterns):
            if 'gallery/' in path_lower or '/imgroot/' in path_lower or host_lower.endswith('fdn.gsmarena.com'):
                return None
            return "Рекомендованный блок"

        if '/reviews/' in page_lower and '/reviews/' in url_lower:
            try:
                base_slug = page_lower.split('/reviews/')[1].split('/')[0]
                target_slug = url_lower.split('/reviews/')[1].split('/')[0]
                if base_slug.split('_')[0] not in target_slug:
                    return "Из другой статьи (reviews)"
            except IndexError:
                pass

        return None

    # ---------- ЗАГОЛОВОК ----------
    def get_article_title(self, soup: BeautifulSoup) -> str:
        title_tags = ['h1', 'title', '.article-title', '.post-title']
        for tag in title_tags:
            element = soup.select_one(tag) if tag.startswith('.') else soup.find(tag)
            if element:
                title = element.get_text().strip()
                if title:
                    return self.clean_filename(title)
        return "article"

    def get_title_from_meta(self, soup: BeautifulSoup) -> Optional[str]:
        title_selectors = ['meta[property="og:title"]', 'meta[name="twitter:title"]', 'title', 'h1']
        for selector in title_selectors:
            element = soup.select_one(selector)
            if element:
                if selector.startswith('meta'):
                    title = element.get('content', '').strip()
                else:
                    title = element.get_text().strip()
                if title:
                    return self.clean_filename(title)
        return None

    # ---------- ПОИСК ИЗОБРАЖЕНИЙ ----------
    def find_content_images(self, soup: BeautifulSoup, url: str) -> List[Dict]:
        images: List[Dict] = []
        hero_normalized: Optional[str] = None
        hero_ids: Set[str] = set()
        hero_stubs: Set[str] = set()
        page_host = urlparse(url).netloc.lower()
        seen_urls: Set[str] = set()
        seen_image_ids: Set[str] = set()
        seen_stubs: Set[str] = set()
        seen_strict_stubs: Set[str] = set()
        json_stubs: Set[str] = set()
        self.logger.debug("=== ПОИСК ОБЛОЖКИ И КОНТЕНТНЫХ ИЗОБРАЖЕНИЙ ===")

        def register(img: Dict, success_label: str, skip_label: str) -> None:
            url_value = img.get('url')
            if not url_value:
                return

            normalized = self._normalize_url(url_value)
            image_id = img.get('image_id') or self._extract_image_id(url_value)
            stub = self._extract_image_stub(url_value)
            is_benchmark = '/benchmarks/' in normalized
            source = img.get('source')
            host = urlparse(normalized).netloc.lower()
            element = img.get('element')
            allow_duplicate = self._allow_duplicate_image(page_host, element, source)

            if hero_normalized and normalized == hero_normalized:
                allow_duplicate = False
            if image_id and image_id in hero_ids:
                allow_duplicate = False
            if stub and stub in hero_stubs:
                allow_duplicate = False

            if normalized in seen_urls and not allow_duplicate:
                self.logger.debug(f"{skip_label}: {normalized}")
                return

            if image_id and image_id in seen_image_ids and not allow_duplicate:
                self.logger.debug(f"{skip_label}: {normalized}")
                return

            if not image_id and stub and stub in seen_stubs and not allow_duplicate:
                self.logger.debug(f"{skip_label}: {normalized}")
                return

            if not image_id and stub and stub in json_stubs and not allow_duplicate:
                self.logger.debug(f"{skip_label}: {normalized}")
                return

            if self._is_recommendation_element(element, page_host, url_value) and source not in {'json_gallery', 'juxtapose'}:
                self.logger.debug(f"{skip_label}: блок рекомендаций")
                return

            if host.endswith('youtube.com') or host.endswith('ytimg.com'):
                self.logger.debug(f"{skip_label}: внешнее превью {host}")
                return

            if is_benchmark and source is None:
                self.logger.debug(f"{skip_label}: контент бенчмарка пропущен")
                return

            if self._is_avatar_block(element):
                self.logger.debug(f"{skip_label}: аватар автора")
                return

            if is_benchmark and stub and stub in seen_strict_stubs:
                self.logger.debug(f"{skip_label}: {normalized}")
                return

            images.append(img)
            seen_urls.add(normalized)
            if image_id:
                seen_image_ids.add(image_id)
            if stub and (not image_id):
                seen_stubs.add(stub)
            if is_benchmark and stub:
                seen_strict_stubs.add(stub)
            if stub and img.get('source') == 'json_gallery':
                json_stubs.add(stub)
            self.logger.debug(f"{success_label}: {normalized}")

        hero_image = self.find_hero_image(soup, url)
        if hero_image:
            hero_image.setdefault('source', 'hero')
            hero_normalized = self._normalize_url(hero_image['url'])
            hero_id = hero_image.get('image_id') or self._extract_image_id(hero_image['url'])
            if hero_id:
                hero_ids.add(hero_id)
                hero_image['image_id'] = hero_id
            hero_stub = self._extract_image_stub(hero_image['url'])
            if hero_stub:
                hero_stubs.add(hero_stub)
            register(hero_image, "Обложка добавлена", "[ПРОПУСК] Дубль обложки")

        # ПОРЯДОК РЕГИСТРАЦИИ КРИТИЧЕН (дедуп/первый source):
        # hero -> JSON-галереи -> Juxtapose -> ZDNet -> gallery-item -> контент
        galleries = self.phonearena.parse_json_galleries(soup)
        if galleries:
            for img in self.phonearena.extract_gallery_images(galleries, url):
                register(img, "[JSON] Добавлено", "[ПРОПУСК] Дубль JSON")

        juxtapose_images = self.juxtapose.extract(soup)
        for img in juxtapose_images:
            register(img, "[Juxtapose] Добавлено", "[ПРОПУСК] Дубль Juxtapose")

        if self.zdnet.matches(page_host):
            for img in self.zdnet.extract(soup):
                register(img, "[ZDNET] Добавлено", "[ПРОПУСК] Дубль ZDNET")

        soup_copy = BeautifulSoup(str(soup), 'html.parser')
        self._remove_excluded_elements(soup_copy, page_host)

        content_area = self._find_content_area(soup_copy)
        if not content_area:
            self.logger.warning("[ERROR] Не найден основной контент статьи")
            return images

        gallery_item_images = self._extract_gallery_item_images(content_area)
        for img in gallery_item_images:
            register(img, "[Gallery item] Добавлено", "[ПРОПУСК] Дубль gallery-item")

        content_images = self._extract_content_images(content_area, url)
        for img in content_images:
            register(img, "[CONTENT] Добавлено", "[ПРОПУСК] Дубль контента")

        self.logger.debug(f"=== ВСЕГО УНИКАЛЬНЫХ ИЗОБРАЖЕНИЙ: {len(images)} ===")
        return images

    def _remove_excluded_elements(self, soup: BeautifulSoup, page_host: Optional[str] = None) -> None:
        excluded_selectors = [
            'header nav', 'footer', 'aside',
            '.sidebar', '.advertisement', '.ad', '.promo',
            '.related-links', '.social-share', '.author-bio',
            '.newsletter', '.subscribe', '.comments',
            '.recommended-articles', '.recommendations-widget',
            '.popular-stories', '.latest-discussions',
            '.trending-articles', '.related-content',
            '.discussion-latest-content',
            '.back-to-top', '.scroll-to-top',
            '.cookie-banner', '.gdpr-banner'
        ]
        for selector in excluded_selectors:
            for element in soup.select(selector):
                element.decompose()

        if page_host and self.zdnet.matches(page_host):
            for selector in self.zdnet.EXCLUDED_SELECTORS:
                for element in soup.select(selector):
                    element.decompose()

    def _find_content_area(self, soup: BeautifulSoup) -> Optional[BeautifulSoup]:
        content_selectors = [
            'article', 'main', '.article', '.post', '.content',
            '.entry-content', '.post-content', '.article-body', '.single-content',
            '#review-body', 'div#review-body', '.review-page', '.review-section', '.review-article'
        ]
        for selector in content_selectors:
            content_area = soup.select_one(selector)
            if content_area:
                self.logger.debug(f"[OK] Найден контент в: {selector}")
                return content_area
        h1 = soup.find('h1')
        if h1 and h1.parent:
            self.logger.debug("[OK] Найден контент в: родитель H1")
            return h1.parent
        return None

    def _extract_content_images(self, content_area: BeautifulSoup, base_url: str) -> List[Dict]:
        images = []
        img_tags = content_area.find_all('img')
        self.logger.debug(f"Изображений в контентной области: {len(img_tags)}")

        for i, img in enumerate(img_tags, 1):
            img_url = self._get_image_url(img)
            if not img_url:
                continue
            alt_text = img.get('alt', '').strip()
            self.logger.debug(f"\n--- Контентное изображение {i} ---\nURL: {img_url}\nALT: {alt_text}")

            if self.is_tracking_pixel(img, img_url):
                continue
            if self.is_author_or_avatar(img, img_url, alt_text):
                self.logger.debug("    [ПРОПУСК] Аватар автора")
                continue
            if self.is_ui_element(img, img_url, alt_text):
                self.logger.debug("    [ПРОПУСК] UI элемент")
                continue

            full_url = urljoin(base_url, img_url)

            skip_reason = self._should_skip_by_url_pattern(full_url, base_url, element=img)
            if skip_reason:
                self.logger.debug(f"    [ПРОПУСК] {skip_reason}")
                continue

            if not self.validate_url(full_url):
                continue

            self.logger.debug("    [OK] Принято: будет скачано")
            images.append({
                'url': full_url,
                'alt': alt_text,
                'element': img,
                'image_id': self._extract_image_id(full_url)
            })

        return images

    def _select_from_srcset(self, srcset: str) -> Optional[str]:
        if not srcset:
            return None
        candidates = []
        for entry in srcset.split(','):
            entry = entry.strip()
            if not entry:
                continue
            parts = entry.split()
            url = parts[0]
            width = 0
            if len(parts) > 1:
                size = parts[-1]
                digits = ''.join(ch for ch in size if ch.isdigit())
                if digits:
                    try:
                        width = int(digits)
                    except ValueError:
                        width = 0
            candidates.append((width, url))
        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0])
        return candidates[-1][1]

    def _get_image_url(self, img_tag) -> Optional[str]:
        attribute_candidates = [
            img_tag.get('data-src'),
            img_tag.get('data-lazy-src'),
            img_tag.get('data-lazy'),
            img_tag.get('data-original'),
            img_tag.get('data-srcset'),
            img_tag.get('src'),
        ]

        first_candidate: Optional[str] = None
        for candidate in attribute_candidates:
            if candidate:
                candidate = candidate.strip()
                if candidate:
                    first_candidate = candidate
                    break

        anchor = img_tag.find_parent('a')
        anchor_candidate: Optional[str] = None
        if anchor:
            anchor_attrs = [
                anchor.get('data-src'),
                anchor.get('data-original'),
                anchor.get('data-full'),
                anchor.get('data-href'),
                anchor.get('href'),
            ]
            for candidate in anchor_attrs:
                if not candidate:
                    continue
                candidate = candidate.strip()
                if not candidate or candidate.startswith('#'):
                    continue
                if self._looks_like_image_url(candidate):
                    anchor_candidate = candidate
                    break

            showimg2_url = self.gsmarena.extract_showimg2(anchor.get('onclick') or '')
            if showimg2_url:
                anchor_candidate = showimg2_url

        if anchor_candidate:
            return anchor_candidate

        if first_candidate:
            return first_candidate

        if anchor:
            # ShowImg2 уже обработан выше через anchor_candidate.
            href = anchor.get('href')
            if href:
                href = href.strip()
                if href and href != '#' and self._looks_like_image_url(href):
                    return href

        picture = img_tag.find_parent('picture')
        if picture:
            picture_candidates = [
                picture.get('data-srcset'),
                picture.get('data-lazy-srcset'),
                picture.get('srcset'),
                picture.get('data-original'),
                picture.get('data-src'),
            ]
            for candidate in picture_candidates:
                if candidate:
                    url = self._select_from_srcset(candidate) if (' ' in candidate or ',' in candidate) else candidate
                    if url:
                        return url

            for source in picture.find_all('source'):
                srcset_value = source.get('data-srcset') or source.get('srcset')
                if srcset_value:
                    url = self._select_from_srcset(srcset_value)
                    if url:
                        return url
                direct_value = source.get('data-src') or source.get('src')
                if direct_value:
                    direct_value = direct_value.strip()
                    if direct_value:
                        return direct_value

        return None

    def _extract_gallery_item_images(self, content_area: BeautifulSoup) -> List[Dict]:
        images = []
        gallery_links = content_area.select('a.gallery-item')
        for link in gallery_links:
            href = link.get('href')
            if not href or not href.startswith('http'):
                continue
            img_tag = link.find('img')
            alt_text = img_tag.get('alt', 'Gallery image') if img_tag else 'Gallery image'
            images.append({
                'url': href,
                'alt': alt_text,
                'element': link,
                'source': 'gallery_item',
                'image_id': self._extract_image_id(href)
            })
        if images:
            self.logger.debug(f"Найдено {len(images)} изображений в gallery-item")
        return images

    def find_hero_image(self, soup: BeautifulSoup, url: str) -> Optional[Dict]:
        self.logger.debug("\n--- ПОИСК ОБЛОЖКИ СТАТЬИ ---")
        hero_selectors = [
            'meta[property="og:image"]',
            'meta[name="twitter:image"]',
            '.hero-image img', '.featured-image img', '.article-hero img',
            '.post-featured-image img', '.main-image img', '.header-image img',
            'header img', '.article-header img', '.post-header img',
            'h1 + * img', 'h1 ~ * img'
        ]
        for selector in hero_selectors:
            self.logger.debug(f"Проверяю: {selector}")
            if selector.startswith('meta'):
                hero_data = self._extract_meta_image(soup, url, selector)
                if hero_data:
                    return hero_data
            else:
                hero_data = self._extract_hero_image(soup, url, selector)
                if hero_data:
                    return hero_data
        self.logger.debug("[ERROR] Обложка не найдена")
        return None

    def _extract_meta_image(self, soup: BeautifulSoup, base_url: str, selector: str) -> Optional[Dict]:
        meta = soup.select_one(selector)
        if meta:
            img_url = meta.get('content')
            if img_url:
                full_url = urljoin(base_url, img_url)
                if self.validate_url(full_url):
                    self.logger.debug(f"[OK] НАЙДЕНА ОБЛОЖКА в мета-тегах: {full_url}")
                    return {
                        'url': full_url,
                        'alt': self.get_title_from_meta(soup) or 'Обложка статьи',
                        'element': meta,
                        'image_id': self._extract_image_id(full_url)
                    }
        return None

    def _extract_hero_image(self, soup: BeautifulSoup, base_url: str, selector: str) -> Optional[Dict]:
        img = soup.select_one(selector)
        if img:
            img_url = self._get_image_url(img)
            if img_url:
                alt_text = img.get('alt', '')
                if not self.is_author_or_avatar(img, img_url, alt_text):
                    full_url = urljoin(base_url, img_url)
                    if self.validate_url(full_url):
                        self.logger.debug(f"[OK] НАЙДЕНА ОБЛОЖКА: {full_url}")
                        return {
                            'url': full_url,
                            'alt': alt_text or 'Обложка статьи',
                            'element': img,
                            'image_id': self._extract_image_id(full_url)
                        }
        return None

    # ---------- ФИЛЬТРЫ ----------
    def is_tracking_pixel(self, img_tag, img_url: str) -> bool:
        tracking_domains = ['googletagmanager', 'facebook.com/tr', 'doubleclick.net/activity']
        img_url_lower = img_url.lower()
        for domain in tracking_domains:
            if domain in img_url_lower:
                self.logger.debug(f"    [ПРОПУСК] Пиксель отслеживания: {domain}")
                return True

        width = img_tag.get('width')
        height = img_tag.get('height')
        if width and height:
            try:
                w, h = int(width), int(height)
                if w <= 2 or h <= 2:
                    self.logger.debug(f"    [ПРОПУСК] Пиксель отслеживания: {w}x{h}")
                    return True
            except ValueError:
                pass
        return False

    def _is_recommendation_element(self, element, page_host: Optional[str] = None, img_url: Optional[str] = None) -> bool:
        if not element:
            return False

        block_keywords = [
            'popular-box', 'popular-data', 'popular-list', 'popular-box__article',
            'sidebar-popular', 'popular-box__article-list', 'pricing', 'widget', 'store',
            'promo', 'trending', 'deal', 'sponsored', 'affiliate', 'ads'
        ]

        ancestor = element
        depth = 0
        img_host = ''
        img_url_lower = (img_url or '').lower()
        if img_url:
            img_host = urlparse(img_url).netloc.lower()

        while ancestor is not None and depth < 5:
            classes = ancestor.get('class', [])
            if isinstance(classes, str):
                classes = [classes]
            class_str = ' '.join(classes).lower()

            if class_str:
                if 'hawk' in class_str and img_host and 'futurecdn.net' in img_host:
                    if 'logos' not in img_url_lower and 'merchant' not in img_url_lower:
                        return False

                if any(keyword in class_str for keyword in block_keywords):
                    return True

            ancestor = ancestor.parent
            depth += 1

        return False

    def _is_avatar_block(self, element) -> bool:
        if not element:
            return False
        ancestor = element
        depth = 0
        while ancestor is not None and depth < 6:
            classes = ' '.join(ancestor.get('class', [])).lower()
            if classes:
                has_avatar_marker = any(marker in classes for marker in ['avatar', 'profile', 'byline'])
                has_context_marker = any(marker in classes for marker in ['author', 'person', 'contributor', 'writer', 'staff'])
                if has_avatar_marker and has_context_marker:
                    return True
            ancestor = ancestor.parent
            depth += 1
        return False

    def is_author_or_avatar(self, img_tag, img_url: str, alt_text: str) -> bool:
        """Проверяет, является ли изображение аватаром автора (улучшенная логика)."""
        alt_lower = (alt_text or "").lower()
        url_lower = (img_url or "").lower()

        content_keywords = [
            'color', 'performance', 'device', 'view', 'comparison',
            'graph', 'benchmark', 'test', 'sample'
        ]
        if any(k in url_lower or k in alt_lower for k in content_keywords):
            return False

        author_phrases = [
            'author photo', 'author avatar', 'writer photo',
            'journalist photo', 'editor photo', 'by author',
            'profile picture', 'headshot', 'staff photo'
        ]
        matched_phrase = next((p for p in author_phrases if p in alt_lower), None)
        if matched_phrase:
            self.logger.debug(f"    [ПРОПУСК] Аватар автора по фразе: {matched_phrase}")
            return True

        strict_avatar_patterns = [
            '/avatar', 'avatar/', '-avatar',
            '/author', 'author/', '-author',
            '/headshot',
            '/staff',
            '/writer',
            '/users/',
            '/byline/',
            '/profile/',
            'user-profile',
            'author-profile',
            '-bio',
        ]
        for pattern in strict_avatar_patterns:
            if pattern in url_lower:
                self.logger.debug(f"    [ПРОПУСК] Аватар автора по URL: {pattern}")
                return True

        if img_tag:
            classes = ' '.join(img_tag.get('class', [])).lower()
            avatar_classes = ['author-avatar', 'author__avatar', 'staff-photo', 'writer-image', 'byline']
            for avatar_class in avatar_classes:
                if avatar_class in classes:
                    self.logger.debug(f"    [ПРОПУСК] Аватар по классу: {avatar_class}")
                    return True

            ancestor = img_tag.parent
            depth = 0
            while ancestor is not None and depth < 4:
                ancestor_classes = ' '.join(ancestor.get('class', [])).lower()
                if ancestor_classes and 'author' in ancestor_classes and ('avatar' in ancestor_classes or 'profile' in ancestor_classes):
                    self.logger.debug("    [ПРОПУСК] Аватар по классу родителя")
                    return True
                ancestor = ancestor.parent
                depth += 1

        return False

    def is_ui_element(self, img_tag, img_url: str, alt_text: str) -> bool:
        # Порядок проверок — инвариант: whitelist по alt перекрывает blacklist'ы
        url_lower = img_url.lower()
        alt_lower = alt_text.lower()

        if any(indicator in alt_lower for indicator in self.UI_CONTENT_ALT_WHITELIST):
            return False

        for pattern in self.UI_URL_BLACKLIST:
            if pattern in url_lower:
                self.logger.debug(f"    [ПРОПУСК] UI элемент в URL: {pattern}")
                return True

        for pattern in self.UI_ALT_BLACKLIST:
            if pattern in alt_lower:
                self.logger.debug(f"    [ПРОПУСК] UI элемент в ALT: {pattern}")
                return True

        if img_tag:
            width = img_tag.get('width')
            height = img_tag.get('height')
            if width and height:
                try:
                    w, h = int(width), int(height)
                    if (w < 50 and h < 50) or (w < 20 or h < 20):
                        self.logger.debug(f"    [ПРОПУСК] Маленькая иконка: {w}x{h}")
                        return True
                except ValueError:
                    pass

        return False

    # ---------- ВАЛИДАЦИЯ/СКАЧИВАНИЕ ----------
    def is_valid_image(self, img_path: str) -> bool:
        """Валидация файла как изображения и проверка минимального размера."""
        try:
            with Image.open(img_path) as img:
                img.verify()

            with Image.open(img_path) as img:
                width, height = img.size

            if width < self.min_size or height < self.min_size:
                self.logger.debug(f"    [ФИЛЬТР] Слишком маленькое: {width}x{height}")
                return False

            self.logger.debug(f"    [OK] Размер OK: {width}x{height}")
            return True

        except Exception as e:
            self.logger.debug(f"    [ОШИБКА] Не удалось открыть изображение: {e}")
            return False

    def _head_probe(self, img_url: str) -> Tuple[Optional[str], Optional[int]]:
        """HEAD-запрос для проверки content-type и content-length."""
        try:
            r = self.session.head(img_url, timeout=15, allow_redirects=True)
            if r.status_code >= 400:
                return None, None
            ctype = (r.headers.get('content-type') or '').lower()
            clen = r.headers.get('content-length')
            clen_int = int(clen) if clen and clen.isdigit() else None
            return ctype, clen_int
        except curl_exceptions.RequestException:
            return None, None

    def download_image(self, img_url: str, save_path: str) -> bool:
        success = False
        try:
            # Предварительный HEAD
            ctype, clen = self._head_probe(img_url)
            if ctype:
                if not ctype.startswith('image/'):
                    self.logger.warning(f"[WARN] Неверный content-type (HEAD): {ctype}")
                    return False
                if 'svg' in ctype:
                    self.logger.warning("[WARN] SVG изображения пропускаются из соображений безопасности (HEAD)")
                    return False
            if clen is not None:
                size_mb = clen / (1024 * 1024)
                if size_mb > self.MAX_FILE_SIZE_MB:
                    self.logger.warning(f"[WARN] Файл слишком большой (HEAD): {size_mb:.2f} MB")
                    return False

            # Основной GET
            response = self.session.get(img_url, timeout=30)
            response.raise_for_status()

            content_type = (response.headers.get('content-type') or '').lower()
            if content_type and not content_type.startswith('image/'):
                self.logger.warning(f"[WARN] Неверный content-type: {content_type}")
                return False
            if content_type and 'svg' in content_type:
                self.logger.warning("[WARN] SVG изображения пропускаются из соображений безопасности")
                return False

            content_length = response.headers.get('content-length')
            if content_length:
                size_mb = int(content_length) / (1024 * 1024)
                if size_mb > self.MAX_FILE_SIZE_MB:
                    self.logger.warning(f"[WARN] Файл слишком большой: {size_mb:.2f} MB")
                    return False

            # Записываем контент
            content = response.content
            if len(content) > self.MAX_FILE_SIZE_MB * 1024 * 1024:
                self.logger.warning("[WARN] Превышен лимит размера при скачивании")
                return False

            hasher = hashlib.sha1(content) if self.hash_dedup else None

            with open(save_path, 'wb') as f:
                f.write(content)

            # Хеш-дедупликация
            if hasher:
                digest = hasher.hexdigest()
                if digest in self._seen_hashes:
                    self.logger.warning("[WARN] Дубликат по содержимому (sha1) — файл удалён")
                    try:
                        os.remove(save_path)
                    except Exception:
                        pass
                    return False
                self._seen_hashes.add(digest)

            # Валидация изображения
            if self.is_valid_image(save_path):
                success = True
                return True
            else:
                os.remove(save_path)
                return False

        except curl_exceptions.Timeout:
            self.logger.error(f"[ERROR] Таймаут при скачивании: {img_url}")
            return False
        except curl_exceptions.HTTPError as e:
            self.logger.error(f"[ERROR] HTTP ошибка: {img_url}")
            return False
        except curl_exceptions.RequestException as e:
            self.logger.error(f"[ERROR] Ошибка сети при скачивании {img_url}: {e}")
            return False
        except KeyboardInterrupt:
            if os.path.exists(save_path):
                try:
                    os.remove(save_path)
                except Exception:
                    pass
            raise
        except IOError as e:
            self.logger.error(f"[ERROR] Ошибка записи файла {save_path}: {e}")
            return False
        except Exception as e:
            self.logger.error(f"[ERROR] Неожиданная ошибка при скачивании {img_url}: {e}")
            return False
        finally:
            if not success and os.path.exists(save_path):
                try:
                    os.remove(save_path)
                except Exception:
                    pass

    # ---------- ПОЛУЧЕНИЕ HTML (с обходом Cloudflare) ----------
    # ВНИМАНИЕ: поддерживать синхронно с CHALLENGE_MARKERS в cf_browser_fetch.py.
    # "cf-mitigated" есть только здесь — НАМЕРЕННО: тут сканируется тело
    # HTTP-ответа (где эта служебная строка встречается), а воркер проверяет
    # только заголовок страницы.
    CHALLENGE_MARKERS = ("just a moment", "verifying you are human",
                         "checking your browser", "cf-mitigated")

    def _looks_like_challenge(self, text: str, status_code: int) -> bool:
        """Похоже ли, что вместо страницы пришла антибот-проверка Cloudflare."""
        if status_code in (403, 429, 503):
            return True
        head = (text or "")[:5000].lower()
        return any(m in head for m in self.CHALLENGE_MARKERS)

    def _browser_fetch(self, url: str) -> Optional[str]:
        """Получить отрендеренный HTML через настоящий браузер (обход CF-проверки)."""
        import subprocess
        import tempfile

        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cf_browser_fetch.py")
        if not os.path.exists(script):
            self.logger.error("[ERROR] cf_browser_fetch.py не найден — обход Cloudflare недоступен")
            return None

        with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as tf:
            out_file = tf.name
        try:
            cmd = [sys.executable, script, url, out_file, "--timeout", str(self.browser_timeout)]

            # На Linux без графической среды запускаем через xvfb-run (без видимого окна).
            # Headless-режим Cloudflare-проверку НЕ проходит, поэтому нужен «настоящий» режим.
            if sys.platform.startswith("linux"):
                from shutil import which
                if which("xvfb-run"):
                    cmd = ["xvfb-run", "-a", "-s", "-screen 0 1366x900x24"] + cmd
                elif not os.environ.get("DISPLAY"):
                    self.logger.warning(
                        "[WARN] Нет xvfb-run и DISPLAY — браузер может не запуститься. "
                        "Установите: sudo apt-get install xvfb")

            self.logger.info("[INFO] Cloudflare-проверка — получаем страницу через браузер…")
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=self.browser_timeout + 60)
            if proc.stderr.strip():
                self.logger.debug(proc.stderr.strip())
            if proc.returncode != 0:
                self.logger.error(f"[ERROR] Браузер не смог пройти проверку (код {proc.returncode})")
                return None
            with open(out_file, "r", encoding="utf-8") as f:
                html = f.read()
            return html or None
        except subprocess.TimeoutExpired:
            self.logger.error("[ERROR] Таймаут браузера при обходе Cloudflare-проверки")
            return None
        except Exception as e:
            self.logger.error(f"[ERROR] Ошибка обхода Cloudflare: {e}")
            return None
        finally:
            try:
                os.remove(out_file)
            except OSError:
                pass

    def _fetch_html(self, url: str) -> Optional[str]:
        """Получить HTML страницы: curl_cffi, а при CF-проверке — браузер.

        Возвращает текст HTML или None.
        """
        if self.browser_fallback == "always":
            html = self._browser_fetch(url)
            if html:
                return html
            self.logger.warning("[WARN] Браузер не отдал HTML, пробуем обычный запрос")

        try:
            response = self.session.get(url, timeout=30)
            status = response.status_code
            text = response.text
        except curl_exceptions.RequestException as exc:
            # сетевая ошибка/блок — пробуем браузер, если разрешено
            if self.browser_fallback != "never":
                self.logger.info(f"[INFO] Запрос не прошёл ({exc}); пробуем браузер")
                return self._browser_fetch(url)
            raise

        if self.browser_fallback != "never" and self._looks_like_challenge(text, status):
            self.logger.info("[INFO] Обнаружена антибот-проверка Cloudflare")
            html = self._browser_fetch(url)
            if html:
                return html
            self.logger.warning("[WARN] Не удалось обойти проверку через браузер")
            return None

        if status >= 400:
            self.logger.error(f"[ERROR] HTTP {status}: {url}")
            return None
        return text

    # ---------- ОСНОВНОЙ ПРОЦЕСС ----------
    def process_article(self, url: str) -> List[str]:
        print(f"\n{'='*70}")
        print(f"Обрабатываем: {url}")
        print(f"{'='*70}")

        if not self.validate_url(url):
            self.logger.error("[ERROR] Невалидный или небезопасный URL")
            return []

        try:
            html = self._fetch_html(url)
            if html is None:
                self.logger.error(f"[ERROR] Не удалось получить страницу: {url}")
                return []

            soup = BeautifulSoup(html, 'html.parser')
            page_urls = self._collect_article_pages(soup, url)

            if len(page_urls) > self.MAX_PAGES:
                self.logger.warning(f"[WARN] Найдено {len(page_urls)} страниц, обрабатываем первые {self.MAX_PAGES}")
                page_urls = page_urls[:self.MAX_PAGES]

            soups = [(soup, url)]
            base_clean = self._strip_query_fragment(url)
            for page_url in page_urls:
                if self._strip_query_fragment(page_url) == base_clean:
                    continue
                try:
                    page_html = self._fetch_html(page_url)
                    if page_html is None:
                        self.logger.warning(f"[WARN] Не удалось загрузить дополнительную страницу: {page_url}")
                        continue
                    soups.append((BeautifulSoup(page_html, 'html.parser'), page_url))
                except curl_exceptions.RequestException as page_error:
                    self.logger.warning(f"[WARN] Не удалось загрузить дополнительную страницу: {page_url} ({page_error})")

            article_title = self.get_article_title(soup)
            if not self.dry_run:
                article_dir = self.create_numbered_article_dir(article_title)

            images = []
            seen_normalized = set()
            for current_soup, current_url in soups:
                page_images = self.find_content_images(current_soup, current_url)
                for image in page_images:
                    normalized = self._normalize_url(image['url'])
                    page_host = urlparse(current_url).netloc.lower()
                    allow_duplicate = self._allow_duplicate_image(page_host, image.get('element'), image.get('source'))
                    if normalized in seen_normalized and not allow_duplicate:
                        continue
                    images.append(image)
                    seen_normalized.add(normalized)

            if not images:
                print('[ERROR] Картинки не найдены')
                return []

            if self.dry_run:
                # Возвращаем нормализованные URL вместо путей к файлам.
                normalized_urls = [self._normalize_url(img['url']) for img in images]
                print(f"\n[DRY-RUN] Найдено {len(normalized_urls)} изображений (ничего не скачано):")
                for n_url in normalized_urls:
                    print(f"  {n_url}")
                return normalized_urls

            print(f"\nНайдено {len(images)} уникальных изображений для скачивания")

            downloaded = self._download_images(images, article_dir)

            print(f"\nУспешно скачано: {len(downloaded)}/{len(images)} изображений")
            return downloaded

        except curl_exceptions.Timeout:
            self.logger.error(f"[ERROR] Таймаут при загрузке страницы: {url}")
            return []
        except curl_exceptions.HTTPError as e:
            self.logger.error(f"[ERROR] HTTP ошибка: {url}")
            return []
        except curl_exceptions.RequestException as e:
            self.logger.error(f"[ERROR] Ошибка при обработке {url}: {e}")
            return []
        except Exception as e:
            self.logger.error(f"[ERROR] Неожиданная ошибка при обработке {url}: {e}")
            return []

    def _download_images(self, images: List[Dict], article_dir: str) -> List[str]:
        downloaded = []
        for i, img_data in enumerate(images, 1):
            img_url = img_data['url']
            alt_text = img_data['alt']
            filename = self._generate_filename(img_url, alt_text, i)
            save_path = os.path.join(article_dir, filename)

            print(f"\n  [{i}/{len(images)}] Скачиваем: {filename}")

            if self.download_image(img_url, save_path):
                downloaded.append(save_path)
                print(f"    [OK] Успешно")
            else:
                print(f"    [ERROR] Ошибка или не прошло фильтр")

            if i < len(images):
                time.sleep(self.pause_between_downloads)
        return downloaded

    def _generate_filename(self, img_url: str, alt_text: str, index: int) -> str:
        parsed_url = urlparse(img_url)
        original_name = os.path.basename(parsed_url.path)
        if not original_name or '.' not in original_name:
            extension = '.jpg'
            name_part = f"image_{index}"
        else:
            name_part, extension = os.path.splitext(original_name)
        if alt_text:
            filename = f"{index:02d}_{self.clean_filename(alt_text)}{extension}"
        else:
            filename = f"{index:02d}_{self.clean_filename(name_part)}{extension}"
        return filename


# ---------- CLI ----------
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Скачивание контентных изображений из статьи (curl_cffi edition)."
    )
    parser.add_argument('url', nargs='?', help='Один URL для обработки (если не указан, используется --urls-file)')
    parser.add_argument('--urls-file', '--urls', dest='urls_file', default='urls.txt',
                        help='Файл со списком URL-ов (по одному в строке) [по умолчанию: urls.txt]')
    parser.add_argument('--download-dir', default='downloaded_images', help='Папка для сохранения')
    parser.add_argument('--min-size', type=int, default=20, help='Минимальный размер картинки в пикселях')
    parser.add_argument('--max-size-mb', type=int, default=50, help='Лимит размера файла (МБ)')
    parser.add_argument('--max-pages', type=int, default=20, help='Максимум страниц для многостраничных статей')
    parser.add_argument('--pause', type=float, default=0.5, help='Пауза между скачиваниями (сек)')
    parser.add_argument('--hash-dedup', action='store_true', help='Включить дедупликацию по хешу содержимого')
    parser.add_argument('--log-file', default=None, help='Писать логи также в файл')
    parser.add_argument('--debug', action='store_true', help='Подробные логи')
    parser.add_argument('--browser-fallback', choices=['auto', 'always', 'never'], default='auto',
                        help="Обход Cloudflare-проверки через браузер: "
                             "auto (только при проверке), always (всегда), never (никогда) "
                             "[по умолчанию: auto]")
    parser.add_argument('--browser-timeout', type=float, default=75.0,
                        help='Сколько секунд ждать прохождения Cloudflare-проверки в браузере')
    parser.add_argument('--dry-run', action='store_true',
                        help='Ничего не скачивать: пройти пайплайн и напечатать URL найденных картинок')
    return parser.parse_args()


def run_single(downloader: ArticleImageDownloader, url: str) -> None:
    downloader.process_article(url)


def run_batch(downloader: ArticleImageDownloader, urls_path: str) -> None:
    try:
        with open(urls_path, 'r', encoding='utf-8') as f:
            urls = [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        print(f"[ERROR] Файл {urls_path} не найден")
        sys.exit(1)

    if not urls:
        print("[ERROR] Файл пуст или не содержит URL")
        sys.exit(1)

    print(f"\n[START] Начинаем обработку {len(urls)} URL(s)\n")

    total_downloaded = 0
    for i, url in enumerate(urls, 1):
        print(f"\n{'#'*70}")
        print(f"# Статья {i}/{len(urls)}")
        print(f"{'#'*70}")
        downloaded = downloader.process_article(url)
        total_downloaded += len(downloaded)

    print(f"\n{'='*70}")
    print(f"ГОТОВО! Всего скачано изображений: {total_downloaded}")
    print(f"{'='*70}\n")


def main():
    args = parse_args()
    downloader = ArticleImageDownloader(
        download_dir=args.download_dir,
        min_size=args.min_size,
        debug=args.debug,
        pause_between_downloads=args.pause,
        max_file_size_mb=args.max_size_mb,
        max_pages=args.max_pages,
        hash_dedup=args.hash_dedup,
        log_file=args.log_file,
        browser_fallback=args.browser_fallback,
        browser_timeout=args.browser_timeout,
        dry_run=args.dry_run,
    )

    if args.url:
        run_single(downloader, args.url)
    else:
        run_batch(downloader, args.urls_file)


if __name__ == "__main__":
    main()
