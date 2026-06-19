# -*- coding: utf-8 -*-
"""Сайт-специфичные экстракторы изображений.

Модуль содержит логику под конкретные сайты, вынесенную из основного
загрузчика. Экстракторы используют общий контекст ArticleImageDownloader
через self.dl: logger, session и shared helpers.

ВАЖНО — ПОРЯДОК ВЫЗОВА КРИТИЧЕН. Оркестратор (find_content_images в
download_images.py) регистрирует картинки строго в порядке:
    hero -> JSON-галереи (PhoneArena) -> Juxtapose -> ZDNet ->
    gallery-item -> контентные <img>
Порядок определяет, какой дубль «выигрывает» при дедупликации и какой
source фиксируется первым. Менять его нельзя без пересъёмки эталона
(tools/golden_check.py).

Диспетчеризация тоже сохранена как в оригинале: часть экстракторов
вызывается на каждой странице и сами решают по содержимому (PhoneArena —
по наличию script#galleries-data, Juxtapose — по iframe), часть — по хосту
(метод matches()).
"""

import json
import re

from html import unescape
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qsl, unquote, urljoin, urlparse

from curl_cffi.requests import exceptions as curl_exceptions


class PhoneArenaExtractor:
    """JSON-галереи PhoneArena: <script id="galleries-data"> -> CDN-ссылки."""

    def __init__(self, dl):
        self.dl = dl  # ArticleImageDownloader: logger, session, хелперы

    def parse_json_galleries(self, soup) -> Dict:
        script_tag = soup.find('script', id='galleries-data')
        if not script_tag:
            return {}
        try:
            galleries_data = json.loads(script_tag.string)
            return galleries_data.get('galleries', {})
        except (json.JSONDecodeError, AttributeError) as e:
            self.dl.logger.debug(f"Ошибка парсинга JSON галерей: {e}")
            return {}

    def extract_gallery_images(self, galleries: Dict, base_url: str) -> List[Dict]:
        images = []
        cdn_url = "https://m-cdn.phonearena.com"

        for gallery_id, gallery_data in galleries.items():
            if 'data' not in gallery_data:
                continue

            self.dl.logger.debug(f"Обработка галереи: {gallery_id}")

            for item in gallery_data['data']:
                if item.get('type') != 'image':
                    continue

                image_id = item.get('image_id')
                name = item.get('name', f'image_{image_id}')
                module = item.get('module', 'reviews')

                if not image_id:
                    continue

                img_url = f"{cdn_url}/images/{module}/{image_id}-image/{name}.webp"
                alt_text = item.get('pageTitle', item.get('name', 'Gallery image'))

                images.append({
                    'url': img_url,
                    'alt': alt_text,
                    'element': None,
                    'source': 'json_gallery',
                    'image_id': str(image_id)
                })

        if images:
            self.dl.logger.debug(f"Извлечено {len(images)} изображений из JSON галерей")

        return images


class JuxtaposeExtractor:
    """Слайдеры «до/после» Juxtapose (iframe cdn.knightlab.com + JSON с S3)."""

    def __init__(self, dl):
        self.dl = dl

    def extract(self, soup) -> List[Dict]:
        if not soup:
            return []

        images: List[Dict] = []
        seen_uids: Set[str] = set()
        attr_candidates = ['src', 'data-src', 'data-lazy-src', 'data-lazy', 'data-original']

        for iframe in soup.find_all('iframe'):
            slider_url = None
            for attr in attr_candidates:
                value = iframe.get(attr)
                if value and 'juxtapose' in value:
                    slider_url = value.strip()
                    break

            if not slider_url:
                continue

            parsed = urlparse(slider_url)
            query_pairs = dict(parse_qsl(parsed.query))
            uid = query_pairs.get('uid') or ''
            uid = unquote(uid).strip()
            if not uid:
                continue

            if uid.endswith('/'):
                uid = uid.rstrip('/')

            if uid.lower().startswith('http'):
                json_url: Optional[str] = uid
            else:
                json_url = f"https://s3.amazonaws.com/uploads.knightlab.com/juxtapose/{uid}.json"

            if json_url in seen_uids:
                continue
            seen_uids.add(json_url)

            try:
                response = self.dl.session.get(json_url, timeout=15)
                response.raise_for_status()
                data = response.json()
            except curl_exceptions.RequestException as exc:
                self.dl.logger.warning(f"[WARN] Juxtapose JSON не загружен: {json_url} ({exc})")
                continue
            except ValueError as exc:
                self.dl.logger.warning(f"[WARN] Ошибка парсинга Juxtapose JSON: {json_url} ({exc})")
                continue

            slider_images = data.get('images', [])
            if not slider_images:
                continue

            self.dl.logger.debug(f"Juxtapose {uid} содержит {len(slider_images)} изображений")
            for item in slider_images:
                src = item.get('src')
                if not src:
                    continue
                label = (item.get('label') or '').strip()
                credit = (item.get('credit') or '').strip()

                if label and credit and credit.lower() not in label.lower():
                    alt_text = f"{label} — {credit}"
                else:
                    alt_text = label or credit or 'Juxtapose image'

                images.append({
                    'url': src,
                    'alt': alt_text,
                    'element': iframe,
                    'source': 'juxtapose'
                })

        return images


class ZDNetExtractor:
    """Инлайн-картинки ZDNet из window.__NUXT__ (shortcode/imagegroup)."""

    # Сайт-специфичные элементы, вырезаемые перед поиском контента
    # (используется в _remove_excluded_elements оркестратора)
    EXCLUDED_SELECTORS = ['[data-component="global-author"]', '.c-globalAuthor']

    def __init__(self, dl):
        self.dl = dl

    def matches(self, host: str) -> bool:
        return host.endswith('zdnet.com')

    def extract(self, soup) -> List[Dict]:
        script_content: Optional[str] = None
        for script in soup.find_all('script'):
            raw = script.string
            if not raw:
                continue
            if 'window.__NUXT__=' in raw:
                script_content = raw
                break

        if not script_content:
            return []

        try:
            decoded = script_content.encode('utf-8').decode('unicode_escape')
        except UnicodeDecodeError:
            return []

        uuid_to_path: Dict[str, str] = {}
        path_pattern = re.compile(r'id:\s*"([0-9a-f\-]{8,})"\s*,.*?path:\s*"([^"]+)"', re.IGNORECASE | re.DOTALL)
        for match in path_pattern.finditer(decoded):
            uuid = match.group(1)
            path = match.group(2)
            uuid_to_path[uuid] = path

        images: List[Dict] = []
        seen_urls: Set[str] = set()
        ordered_entries: List[Tuple[int, Dict]] = []
        shortcode_pattern = re.compile(r'<shortcode\s+shortcode="image"\s+([^>]+)>', re.IGNORECASE)
        attr_pattern = re.compile(r'([A-Za-z0-9_-]+)="([^"]*)"')

        for match in shortcode_pattern.finditer(decoded):
            attr_text = match.group(1)
            attrs = {key: unescape(value) for key, value in attr_pattern.findall(attr_text)}
            uuid = attrs.get('uuid')
            if not uuid:
                continue
            url = uuid_to_path.get(uuid)
            if not url:
                filename = attrs.get('image-filename')
                if filename:
                    candidate = re.search(rf'https://www\.zdnet\.com/a/img/[^\s"]+/{re.escape(filename)}', decoded)
                    if candidate:
                        url = candidate.group(0)
            if not url:
                filename = attrs.get('image-filename')
                date_created = attrs.get('image-date-created', '').strip()
                if filename and date_created:
                    date_path = '/'.join(part.strip() for part in date_created.split('/') if part.strip())
                    if date_path:
                        url = f"https://www.zdnet.com/a/img/{date_path}/{uuid}/{filename}"
            if not url:
                continue
            ordered_entries.append((match.start(), {
                'url': url,
                'alt': attrs.get('image-alt-text', ''),
                'element': None,
                'source': 'zdnet_nuxt',
                'image_id': None,
            }))

        imagegroup_pattern = re.compile(r'imagegroup="([^"]+)"', re.IGNORECASE)
        for match in imagegroup_pattern.finditer(decoded):
            raw = unescape(match.group(1))
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue

            image_data = data.get('imageData') or {}
            url = image_data.get('path') or data.get('path')
            if not url:
                filename = image_data.get('filename') or data.get('imageFilename')
                date_created = data.get('imageDateCreated')
                uuid = data.get('uuid') or image_data.get('id')
                if filename and date_created and uuid:
                    date_path = '/'.join(part.strip() for part in date_created.replace('-', '/').split('/') if part.strip())
                    if date_path:
                        url = f"https://www.zdnet.com/a/img/{date_path}/{uuid}/{filename}"

            if not url:
                continue

            ordered_entries.append((match.start(), {
                'url': url,
                'alt': data.get('imageAltText') or data.get('alt') or image_data.get('alt', ''),
                'element': None,
                'source': 'zdnet_imagegroup',
                'image_id': image_data.get('id') or data.get('uuid'),
            }))

        ordered_entries.sort(key=lambda entry: entry[0])
        for _, img_data in ordered_entries:
            url = img_data['url']
            if not self.dl.validate_url(url):
                continue
            if url in seen_urls:
                continue
            seen_urls.add(url)
            images.append(img_data)

        return images


class GSMArenaExtractor:
    """GSM Arena: полноразмерные картинки (onclick=ShowImg2) и доменные правила стабов."""

    def __init__(self, dl):
        self.dl = dl

    def matches(self, host: str) -> bool:
        return host.endswith('gsmarena.com')

    def extract_showimg2(self, onclick: str) -> Optional[str]:
        """Разбор onclick="ShowImg2('...')" — полноразмерные картинки GSM Arena."""
        if not onclick:
            return None
        match = re.search(r'ShowImg2\(\s*[\'"]([^\'"]+)[\'"]\s*\)', onclick)
        if not match:
            return None
        path = match.group(1).strip()
        if not path:
            return None
        return path if path.startswith('http') else urljoin('https://fdn.gsmarena.com/imgroot/', path.lstrip('/'))

    def augment_stub(self, parsed_url, stub: str) -> str:
        """Дополняет стаб именем папки из пути (дедуп одноимённых файлов разных галерей)."""
        segments = [segment for segment in parsed_url.path.split('/') if segment]
        if len(segments) < 2:
            return stub
        folder_hint = None
        size_pattern = re.compile(r'-?\d+(?:x\d+)?(?:w\d+)?$')
        for segment in reversed(segments[:-1]):
            if size_pattern.fullmatch(segment):
                continue
            if segment.lower() in {'images', 'imgroot', 'review', 'reviews'}:
                continue
            folder_hint = segment
            break
        if not folder_hint:
            candidate = segments[-2]
            if size_pattern.fullmatch(candidate) and len(segments) >= 3:
                candidate = segments[-3]
            folder_hint = candidate
        if folder_hint:
            return f"{folder_hint.lower()}_{stub}"
        return stub


class TomsHardwarePaginator:
    """Tom's Hardware: сбор страниц многостраничной статьи (rel=next/prev и т.п.)."""

    def __init__(self, dl):
        self.dl = dl

    def matches(self, host: str) -> bool:
        return host.endswith('tomshardware.com')

    def collect_pages(self, soup, base_url: str) -> Set[str]:
        pages = set()
        base_clean = self.dl._strip_query_fragment(base_url)
        pages.add(base_clean)

        def add_link(href: Optional[str]) -> None:
            if not href:
                return
            absolute = urljoin(base_url, href)
            absolute_clean = self.dl._strip_query_fragment(absolute)
            pages.add(absolute_clean)

        pagination_selectors = [
            'a[rel="next"]', 'a[rel="prev"]',
            'a.next-page', 'a.pagination__next', 'a.pagination__prev',
            'a[data-page]',
        ]

        for selector in pagination_selectors:
            for link in soup.select(selector):
                add_link(link.get('href'))

        for link_tag in soup.find_all('link'):
            rel = link_tag.get('rel') or []
            if any(value.lower() in {'next', 'prev'} for value in rel):
                add_link(link_tag.get('href'))

        return pages
