# -*- coding: utf-8 -*-
"""
Характеризационный (characterization) тест метода
ArticleImageDownloader.is_ui_element.

ВНИМАНИЕ: этот тест фиксирует ТЕКУЩЕЕ поведение метода is_ui_element
как эталон ("золотой снимок") перед изменениями. Ожидаемые значения
(expected) получены ПУТЁМ РЕАЛЬНОГО ВЫЗОВА текущей реализации метода,
а не выведены вручную.

ЭТОТ ФАЙЛ НЕЛЬЗЯ ПРАВИТЬ ПРИ ИЗМЕНЕНИИ is_ui_element. Он должен
оставаться зелёным без единого изменения — любые расхождения означают,
что поведение метода изменилось.

Примечание: в коде is_ui_element список ui_url_patterns на момент
написания теста содержит 21 элемент (а не 16, как можно было бы
предположить по устаревшему описанию) — все 21 паттерн покрыты ниже.
"""

import pytest
from bs4 import BeautifulSoup

from download_images import ArticleImageDownloader


@pytest.fixture
def downloader(tmp_path):
    """Экземпляр ArticleImageDownloader с отключённым debug-логированием."""
    return ArticleImageDownloader(download_dir=str(tmp_path), debug=False)


def make_img_tag(html):
    """Строит BeautifulSoup-тег <img ...> из HTML-фрагмента (или None)."""
    if html is None:
        return None
    return BeautifulSoup(html, 'html.parser').img


# ---------------------------------------------------------------------------
# Таблица кейсов: (img_tag_html, img_url, alt_text, expected, описание)
#
# img_tag_html=None означает img_tag=None в вызове is_ui_element.
# expected проверены реальным запуском текущей реализации метода.
# ---------------------------------------------------------------------------
CASES = [
    # === 1) Каждый паттерн ui_url_patterns (21 шт., по одному срабатыванию) ===
    ('<img src="x">', 'https://example.com/button/img.png', '', True, "url-паттерн '/button/'"),
    ('<img src="x">', 'https://example.com/btn/img.png', '', True, "url-паттерн '/btn/'"),
    ('<img src="x">', 'https://example.com/icon/img.png', '', True, "url-паттерн '/icon/'"),
    ('<img src="x">', 'https://example.com/logo/img.png', '', True, "url-паттерн '/logo/'"),
    ('<img src="x">', 'https://example.com/badge/img.png', '', True, "url-паттерн '/badge/'"),
    ('<img src="x">', 'https://example.com/button-img.png', '', True, "url-паттерн 'button-'"),
    ('<img src="x">', 'https://example.com/my-btn-img.png', '', True, "url-паттерн '-btn-'"),
    ('<img src="x">', 'https://example.com/icon-img.png', '', True, "url-паттерн 'icon-'"),
    ('<img src="x">', 'https://example.com/logo-img.png', '', True, "url-паттерн 'logo-'"),
    ('<img src="x">', 'https://example.com/google-news-img.png', '', True, "url-паттерн 'google-news'"),
    ('<img src="x">', 'https://example.com/follow-img.png', '', True, "url-паттерн 'follow'"),
    ('<img src="x">', 'https://example.com/subscribe-img.png', '', True, "url-паттерн 'subscribe'"),
    ('<img src="x">', 'https://example.com/social-img.png', '', True, "url-паттерн 'social-'"),
    ('<img src="x">', 'https://example.com/share-img.png', '', True, "url-паттерн 'share-'"),
    ('<img src="x">', 'https://example.com/arrow-right.png', '', True, "url-паттерн 'arrow-'"),
    ('<img src="x">', 'https://example.com/chevron-down.png', '', True, "url-паттерн 'chevron'"),
    ('<img src="x">', 'https://example.com/spinner.gif', '', True, "url-паттерн 'spinner'"),
    ('<img src="x">', 'https://example.com/loader.gif', '', True, "url-паттерн 'loader'"),
    ('<img src="x">', 'https://example.com/placeholder.png', '', True, "url-паттерн 'placeholder'"),
    ('<img src="x">', 'https://example.com/bg-image.jpg', '', True, "url-паттерн 'bg-'"),
    ('<img src="x">', 'https://example.com/watermark.png', '', True, "url-паттерн 'watermark'"),

    # === 2) Каждый паттерн ui_alt_patterns (по одному срабатыванию) ===
    ('<img src="x">', 'https://example.com/img.png', 'follow us', True, "alt-паттерн 'follow us'"),
    ('<img src="x">', 'https://example.com/img.png', 'subscribe', True, "alt-паттерн 'subscribe'"),
    ('<img src="x">', 'https://example.com/img.png', 'share button', True, "alt-паттерн 'share button'"),
    ('<img src="x">', 'https://example.com/img.png', 'click here', True, "alt-паттерн 'click here'"),
    ('<img src="x">', 'https://example.com/img.png', 'download button', True, "alt-паттерн 'download button'"),
    ('<img src="x">', 'https://example.com/img.png', 'menu icon', True, "alt-паттерн 'menu icon'"),
    ('<img src="x">', 'https://example.com/img.png', 'close button', True, "alt-паттерн 'close button'"),
    ('<img src="x">', 'https://example.com/img.png', 'next arrow', True, "alt-паттерн 'next arrow'"),
    ('<img src="x">', 'https://example.com/img.png', 'previous arrow', True, "alt-паттерн 'previous arrow'"),

    # === 3) Отдельный кейс 'newsletter' в alt ===
    ('<img src="x">', 'https://example.com/img.png', 'Newsletter graphic', True, "'newsletter' в alt"),

    # === 4) Whitelist (content_indicators) побеждает blacklist ===
    # alt с контент-индикатором + URL '/icon/...' -> whitelist должен победить (False)
    ('<img src="x">', 'https://example.com/icon/phone.png', 'review of smartphone', False,
     "whitelist побеждает: 'review of smartphone' + '/icon/'"),

    # Каждый content_indicator хотя бы по разу (тоже с URL '/icon/phone.png')
    ('<img src="x">', 'https://example.com/icon/phone.png', 'review', False, "content_indicator 'review'"),
    ('<img src="x">', 'https://example.com/icon/phone.png', 'phone', False, "content_indicator 'phone'"),
    ('<img src="x">', 'https://example.com/icon/phone.png', 'device', False, "content_indicator 'device'"),
    ('<img src="x">', 'https://example.com/icon/phone.png', 'smartphone', False, "content_indicator 'smartphone'"),
    ('<img src="x">', 'https://example.com/icon/phone.png', 'laptop', False, "content_indicator 'laptop'"),
    ('<img src="x">', 'https://example.com/icon/phone.png', 'side button', False, "content_indicator 'side button'"),
    ('<img src="x">', 'https://example.com/icon/phone.png', 'volume', False, "content_indicator 'volume'"),
    ('<img src="x">', 'https://example.com/icon/phone.png', 'power button', False, "content_indicator 'power button'"),
    ('<img src="x">', 'https://example.com/icon/phone.png', 'physical', False, "content_indicator 'physical'"),
    ('<img src="x">', 'https://example.com/icon/phone.png', 'product', False, "content_indicator 'product'"),
    ('<img src="x">', 'https://example.com/icon/phone.png', 'gadget', False, "content_indicator 'gadget'"),
    ('<img src="x">', 'https://example.com/icon/phone.png', 'hardware', False, "content_indicator 'hardware'"),

    # === 5) Размеры из img_tag (с нейтральным URL и пустым alt) ===
    ('<img src="x" width="16" height="16">', 'https://example.com/photo.jpg', '', True, "размер 16x16 (маленькая иконка)"),
    ('<img src="x" width="40" height="40">', 'https://example.com/photo.jpg', '', True, "размер 40x40 (оба < 50)"),
    ('<img src="x" width="19" height="100">', 'https://example.com/photo.jpg', '', True, "размер 19x100 (width < 20)"),
    ('<img src="x" width="100" height="19">', 'https://example.com/photo.jpg', '', True, "размер 100x19 (height < 20)"),
    ('<img src="x" width="60" height="60">', 'https://example.com/photo.jpg', '', False, "размер 60x60 (норм)"),
    ('<img src="x" width="abc" height="10">', 'https://example.com/photo.jpg', '', False, "width='abc' (ValueError -> игнор)"),
    ('<img src="x">', 'https://example.com/photo.jpg', '', False, "без атрибутов размера"),
    ('<img src="x" width="40">', 'https://example.com/photo.jpg', '', False, "только width без height"),

    # === 6) Прочие граничные случаи ===
    ('<img src="x">', 'https://example.com/photo.jpg', '', False, "пустой alt + нейтральный URL"),
    ('<img src="x">', '', '', False, "пустой img_url"),
    (None, 'https://example.com/photo.jpg', '', False, "img_tag=None, нейтральный URL"),
    (None, '', '', False, "img_tag=None, пустой img_url, пустой alt"),
]


@pytest.mark.parametrize(
    'img_tag_html, img_url, alt_text, expected, description',
    CASES,
    ids=[c[-1] for c in CASES],
)
def test_is_ui_element_characterization(downloader, img_tag_html, img_url, alt_text, expected, description):
    """Фиксирует текущее поведение is_ui_element для заданного набора входов."""
    img_tag = make_img_tag(img_tag_html)
    result = downloader.is_ui_element(img_tag, img_url, alt_text)
    assert result is expected, (
        f"{description}: ожидалось {expected!r}, получено {result!r} "
        f"(img_url={img_url!r}, alt={alt_text!r})"
    )
