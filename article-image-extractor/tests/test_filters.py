# -*- coding: utf-8 -*-
"""
Тесты фильтрации ArticleImageDownloader — используют только реальный класс,
без дублирования констант.
"""

import pytest
from bs4 import BeautifulSoup

from download_images import ArticleImageDownloader


# ---------------------------------------------------------------------------
# Фикстура: один экземпляр downloader с tmp-директорией
# ---------------------------------------------------------------------------

@pytest.fixture
def d(tmp_path):
    """Экземпляр ArticleImageDownloader с отключённым debug."""
    return ArticleImageDownloader(download_dir=str(tmp_path), debug=False)


# ===========================================================================
# А) _should_skip_by_url_pattern — рекламные/баннерные паттерны
# ===========================================================================

class TestShouldSkipByUrlPattern:
    """Прямые проверки метода _should_skip_by_url_pattern."""

    def test_ads_в_пути_режется(self, d):
        """/ads/ в URL → метод возвращает строку-причину (не None)."""
        result = d._should_skip_by_url_pattern(
            'https://example.com/ads/banner.jpg',
            'https://example.com/'
        )
        assert result is not None

    def test_banner_в_пути_режется(self, d):
        """/banner в URL → пропускается."""
        result = d._should_skip_by_url_pattern(
            'https://example.com/banner.png',
            'https://example.com/'
        )
        assert result is not None

    def test_static_stores_режется(self, d):
        """/static/stores/ → рекламный паттерн."""
        result = d._should_skip_by_url_pattern(
            'https://example.com/static/stores/logo.jpg',
            'https://example.com/'
        )
        assert result is not None

    def test_arenaev_com_режется(self, d):
        """arenaev.com в URL → рекламный домен."""
        result = d._should_skip_by_url_pattern(
            'https://arenaev.com/img.jpg',
            'https://example.com/'
        )
        assert result is not None

    def test_vv_bigpic_без_родителя_режется(self, d):
        """/vv/bigpic/ без <p class='image-row'> → режется как реклама."""
        img = BeautifulSoup(
            '<img src="https://fdn2.gsmarena.com/vv/bigpic/phone.jpg">',
            'html.parser'
        ).find('img')
        result = d._should_skip_by_url_pattern(
            'https://fdn2.gsmarena.com/vv/bigpic/phone.jpg',
            'https://www.gsmarena.com/',
            element=img
        )
        assert result is not None

    def test_vv_bigpic_с_image_row_пропускается(self, d):
        """Если img лежит в <p class='image-row'>, /vv/bigpic/ НЕ режется (возвращает None)."""
        soup = BeautifulSoup(
            '<p class="image-row"><img src="https://fdn2.gsmarena.com/vv/bigpic/phone.jpg"></p>',
            'html.parser'
        )
        img = soup.find('img')
        result = d._should_skip_by_url_pattern(
            'https://fdn2.gsmarena.com/vv/bigpic/phone.jpg',
            'https://www.gsmarena.com/',
            element=img
        )
        assert result is None

    def test_imgroot_с_news_пропускается(self, d):
        """/imgroot/ в пути перекрывает recommendation_pattern /news/ → None."""
        result = d._should_skip_by_url_pattern(
            'https://fdn.gsmarena.com/imgroot/news/25/01/phone/preview.jpg',
            'https://www.gsmarena.com/'
        )
        assert result is None

    def test_fdn_gsmarena_host_с_news_пропускается(self, d):
        """Хост fdn.gsmarena.com перекрывает /news/ → None."""
        result = d._should_skip_by_url_pattern(
            'https://fdn.gsmarena.com/news/honor-device.jpg',
            'https://www.gsmarena.com/'
        )
        assert result is None


# ===========================================================================
# Б) is_ui_element
# ===========================================================================

class TestIsUiElement:
    """Проверки метода is_ui_element."""

    def test_icon_в_url_режется(self, d):
        """/icon/ в URL → UI-элемент."""
        img = BeautifulSoup(
            '<img src="https://example.com/icon/settings.png" alt="">',
            'html.parser'
        ).find('img')
        assert d.is_ui_element(img, 'https://example.com/icon/settings.png', '') is True

    def test_logo_dash_в_url_режется(self, d):
        """logo- в URL → UI-элемент."""
        img = BeautifulSoup(
            '<img src="https://example.com/images/logo-site.png" alt="">',
            'html.parser'
        ).find('img')
        assert d.is_ui_element(img, 'https://example.com/images/logo-site.png', '') is True

    def test_content_alt_review_спасает(self, d):
        """alt с 'review' спасает изображение от UI-фильтра даже при /icon/ в URL."""
        img = BeautifulSoup(
            '<img src="https://example.com/icon/phone.png" alt="review of smartphone">',
            'html.parser'
        ).find('img')
        assert d.is_ui_element(img, 'https://example.com/icon/phone.png', 'review of smartphone') is False

    def test_content_alt_smartphone_спасает(self, d):
        """alt с 'smartphone' спасает от UI-фильтра."""
        img = BeautifulSoup(
            '<img src="https://example.com/icon/x.png" alt="smartphone camera">',
            'html.parser'
        ).find('img')
        assert d.is_ui_element(img, 'https://example.com/icon/x.png', 'smartphone camera') is False

    def test_маленький_размер_16x16_режется(self, d):
        """width=16 height=16 → маленькая иконка, UI-фильтр."""
        img = BeautifulSoup(
            '<img src="https://example.com/img.png" width="16" height="16" alt="">',
            'html.parser'
        ).find('img')
        assert d.is_ui_element(img, 'https://example.com/img.png', '') is True


# ===========================================================================
# В) is_author_or_avatar
# ===========================================================================

class TestIsAuthorOrAvatar:
    """Проверки метода is_author_or_avatar."""

    def test_avatar_в_url_режется(self, d):
        """/avatar в URL → аватар автора."""
        img = BeautifulSoup(
            '<img src="https://example.com/avatar/john.jpg" alt="">',
            'html.parser'
        ).find('img')
        assert d.is_author_or_avatar(img, 'https://example.com/avatar/john.jpg', '') is True

    def test_alt_author_photo_режется(self, d):
        """alt='author photo' → аватар автора."""
        img = BeautifulSoup(
            '<img src="https://example.com/photo.jpg" alt="author photo">',
            'html.parser'
        ).find('img')
        assert d.is_author_or_avatar(img, 'https://example.com/photo.jpg', 'author photo') is True

    def test_benchmark_в_url_спасает(self, d):
        """'benchmark' в URL → content_keyword, спасает от аватар-фильтра."""
        img = BeautifulSoup(
            '<img src="https://example.com/avatar/benchmark-test.jpg" alt="">',
            'html.parser'
        ).find('img')
        assert d.is_author_or_avatar(img, 'https://example.com/avatar/benchmark-test.jpg', '') is False

    def test_sample_в_alt_спасает(self, d):
        """'sample' в alt → content_keyword, спасает от аватар-фильтра."""
        img = BeautifulSoup(
            '<img src="https://example.com/author/jane.jpg" alt="camera sample">',
            'html.parser'
        ).find('img')
        assert d.is_author_or_avatar(img, 'https://example.com/author/jane.jpg', 'camera sample') is False


# ===========================================================================
# Г) _is_recommendation_element
# ===========================================================================

class TestIsRecommendationElement:
    """Проверки метода _is_recommendation_element."""

    def test_popular_box_возвращает_true(self, d):
        """img внутри div.popular-box → рекомендательный блок."""
        soup = BeautifulSoup(
            '<div class="popular-box"><img src="img.jpg"></div>',
            'html.parser'
        )
        img = soup.find('img')
        assert d._is_recommendation_element(img) is True

    def test_обычный_div_возвращает_false(self, d):
        """img внутри нейтрального div → не рекомендательный блок."""
        soup = BeautifulSoup(
            '<div class="article-content"><img src="img.jpg"></div>',
            'html.parser'
        )
        img = soup.find('img')
        assert d._is_recommendation_element(img) is False

    def test_none_element_возвращает_false(self, d):
        """element=None → False (граничный случай)."""
        assert d._is_recommendation_element(None) is False
