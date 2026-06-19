# -*- coding: utf-8 -*-
"""
Юнит-тесты для детерминированных хелперов ArticleImageDownloader.
Тесты-характеристики: фиксируют ТЕКУЩЕЕ поведение кода, не идеальное.
Все ожидаемые значения верифицированы запуском реального кода.
"""

import os
import sys

# Добавляем родительскую папку в путь, чтобы импортировать download_images
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(TESTS_DIR)
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

import pytest
from bs4 import BeautifulSoup

from download_images import ArticleImageDownloader


@pytest.fixture
def dl(tmp_path):
    """Экземпляр загрузчика с временной папкой; сеть не используется."""
    return ArticleImageDownloader(download_dir=str(tmp_path), debug=False)


# ============================================================
# _normalize_url
# ============================================================

class TestNormalizeUrl:
    """Тесты нормализации URL: схлопывание слешей, удаление фрагментов,
    фильтрация размерных query-параметров, lowercase схемы и хоста."""

    def test_empty_string(self, dl):
        """Пустая строка → пустая строка."""
        assert dl._normalize_url('') == ''

    def test_whitespace_only(self, dl):
        """Строка из пробелов → пустая строка (strip без пути)."""
        assert dl._normalize_url('  ') == ''

    def test_double_slashes_collapsed(self, dl):
        """Повторные слеши в пути схлопываются в один."""
        assert dl._normalize_url('https://example.com/path//to//img.jpg') == \
               'https://example.com/path/to/img.jpg'

    def test_triple_slashes_collapsed(self, dl):
        """Тройные слеши тоже схлопываются."""
        assert dl._normalize_url('https://example.com/path///to///img.jpg') == \
               'https://example.com/path/to/img.jpg'

    def test_fragment_removed(self, dl):
        """Фрагмент (#section) отрезается."""
        assert dl._normalize_url('https://example.com/img.jpg#section') == \
               'https://example.com/img.jpg'

    def test_scheme_lowercased(self, dl):
        """Схема и хост приводятся к нижнему регистру."""
        assert dl._normalize_url('HTTPS://Example.COM/img.jpg') == \
               'https://example.com/img.jpg'

    @pytest.mark.parametrize("param", ['w', 'h', 'width', 'height', 'size',
                                        'resize', 'quality', 'format', 'webp',
                                        'jpeg', 'crop'])
    def test_size_query_param_removed(self, dl, param):
        """Каждый размерный query-параметр должен быть отфильтрован."""
        url = f'https://example.com/img.jpg?{param}=800'
        assert dl._normalize_url(url) == 'https://example.com/img.jpg'

    def test_non_size_query_kept(self, dl):
        """Не-размерный параметр сохраняется."""
        assert dl._normalize_url('https://example.com/img.jpg?keep=me') == \
               'https://example.com/img.jpg?keep=me'

    def test_mixed_query_only_non_size_kept(self, dl):
        """Из смешанного запроса остаётся только не-размерный параметр."""
        assert dl._normalize_url('https://example.com/img.jpg?w=800&h=600&other=yes') == \
               'https://example.com/img.jpg?other=yes'

    def test_empty_query_string(self, dl):
        """URL с пустым query (?) нормализуется без символа ?."""
        assert dl._normalize_url('https://example.com/img.jpg?') == \
               'https://example.com/img.jpg'

    def test_path_not_lowercased(self, dl):
        """Путь и query НЕ приводятся к нижнему регистру (только схема и хост)."""
        result = dl._normalize_url('https://Example.COM/IMG.jpg?keep=yes')
        assert result == 'https://example.com/IMG.jpg?keep=yes'


# ============================================================
# _select_from_srcset
# ============================================================

class TestSelectFromSrcset:
    """Тесты выбора URL с максимальной шириной из srcset."""

    def test_none_input(self, dl):
        """None → None."""
        assert dl._select_from_srcset(None) is None

    def test_empty_string(self, dl):
        """Пустая строка → None."""
        assert dl._select_from_srcset('') is None

    def test_single_url_no_descriptor(self, dl):
        """Единственный URL без дескриптора — возвращается он же."""
        assert dl._select_from_srcset('https://a.com/img.jpg') == 'https://a.com/img.jpg'

    def test_single_url_with_spaces(self, dl):
        """URL с пробелами по краям — обрезается и возвращается."""
        assert dl._select_from_srcset('  https://a.com/only.jpg  ') == 'https://a.com/only.jpg'

    def test_two_candidates_returns_largest(self, dl):
        """Из двух кандидатов выбирается тот, у кого больше ширина."""
        src = 'https://a.com/small.jpg 480w, https://a.com/large.jpg 1200w'
        assert dl._select_from_srcset(src) == 'https://a.com/large.jpg'

    def test_three_candidates_returns_largest(self, dl):
        """Из трёх кандидатов выбирается тот, у кого максимальная ширина."""
        src = 'https://a.com/s.jpg 480w, https://a.com/m.jpg 800w, https://a.com/l.jpg 1200w'
        assert dl._select_from_srcset(src) == 'https://a.com/l.jpg'

    def test_density_descriptor(self, dl):
        """При дескрипторе 1x/2x выбирается тот, у которого число больше."""
        src = 'https://a.com/1x.jpg 1x, https://a.com/2x.jpg 2x'
        assert dl._select_from_srcset(src) == 'https://a.com/2x.jpg'

    def test_no_space_between_entries(self, dl):
        """Кандидаты без пробела вокруг запятой разбираются корректно."""
        src = 'https://a.com/a.jpg 640w,https://a.com/b.jpg 320w'
        assert dl._select_from_srcset(src) == 'https://a.com/a.jpg'


# ============================================================
# _extract_image_id
# ============================================================

class TestExtractImageId:
    """Тесты извлечения числового ID изображения из URL."""

    def test_none_input(self, dl):
        """None → None."""
        assert dl._extract_image_id(None) is None

    def test_empty_string(self, dl):
        """Пустая строка → None."""
        assert dl._extract_image_id('') is None

    def test_no_id_in_url(self, dl):
        """URL без числового ID → None."""
        assert dl._extract_image_id('https://example.com/images/photo.jpg') is None

    def test_pattern_image_suffix(self, dl):
        """Паттерн /12345-image/ → '12345'."""
        url = 'https://m-cdn.phonearena.com/images/reviews/12345-image/photo.webp'
        assert dl._extract_image_id(url) == '12345'

    def test_pattern_number_dash_number(self, dl):
        """Паттерн /12345-67890/ → '12345'."""
        url = 'https://m-cdn.phonearena.com/images/reviews/12345-67890/photo.webp'
        assert dl._extract_image_id(url) == '12345'

    def test_pattern_image_suffix_trailing_slash(self, dl):
        """Паттерн /12345-image/ с финальным слешем → '12345'."""
        url = 'https://example.com/images/12345-image/'
        assert dl._extract_image_id(url) == '12345'

    def test_pattern_number_dash_name_jpg(self, dl):
        """Паттерн /12345-photo.jpg → '12345'."""
        url = 'https://example.com/images/12345-photo.jpg'
        assert dl._extract_image_id(url) == '12345'

    def test_pattern_number_underscore_name_jpg(self, dl):
        """Паттерн /12345_photo.jpg → '12345'."""
        url = 'https://example.com/images/12345_photo.jpg'
        assert dl._extract_image_id(url) == '12345'


# ============================================================
# _extract_image_stub
# ============================================================

class TestExtractImageStub:
    """Тесты извлечения «заглушки» (имя файла без расширения и размерных суффиксов)."""

    def test_none_input(self, dl):
        """None → None."""
        assert dl._extract_image_stub(None) is None

    def test_empty_string(self, dl):
        """Пустая строка → None."""
        assert dl._extract_image_stub('') is None

    def test_no_filename_in_path(self, dl):
        """URL без имени файла (только /path/) → None."""
        assert dl._extract_image_stub('https://example.com/') is None

    def test_basic_stub(self, dl):
        """Обычное имя файла возвращается в нижнем регистре без расширения."""
        assert dl._extract_image_stub('https://example.com/images/my-photo.jpg') == 'my-photo'

    def test_suffix_1200_80_stripped(self, dl):
        """Суффикс -1200-80 отрезается."""
        assert dl._extract_image_stub('https://example.com/images/photo-1200-80.jpg') == 'photo'

    def test_suffix_800x600_stripped(self, dl):
        """Суффикс -800x600 отрезается."""
        assert dl._extract_image_stub('https://example.com/images/photo-800x600.jpg') == 'photo'

    def test_suffix_1024x768_stripped(self, dl):
        """Суффикс -1024x768 отрезается."""
        assert dl._extract_image_stub('https://example.com/images/photo-1024x768.jpg') == 'photo'

    def test_short_suffix_not_stripped(self, dl):
        """Суффиксы с короткими числами (< 3 цифр) НЕ отрезаются."""
        assert dl._extract_image_stub('https://example.com/images/photo-80x60.jpg') == 'photo-80x60'

    def test_gsmarena_prefix_from_folder(self, dl):
        """Для gsmarena.com в stub добавляется имя папки как префикс."""
        url = 'https://fdn.gsmarena.com/images/review/samsung-galaxy-s24/photo.jpg'
        assert dl._extract_image_stub(url) == 'samsung-galaxy-s24_photo'

    def test_gsmarena_skips_review_segment(self, dl):
        """Для gsmarena.com сегмент 'review' пропускается, берётся следующий."""
        url = 'https://fdn.gsmarena.com/images/review/photo.jpg'
        assert dl._extract_image_stub(url) == 'review_photo'

    def test_gsmarena_imgroot_prefix(self, dl):
        """Для gsmarena.com с imgroot используется предпоследний сегмент как префикс."""
        url = 'https://fdn.gsmarena.com/imgroot/samsung-galaxy/main-photo.jpg'
        assert dl._extract_image_stub(url) == 'samsung-galaxy_main-photo'


# ============================================================
# clean_filename
# ============================================================

class TestCleanFilename:
    """Тесты очистки имени файла от запрещённых символов Windows."""

    def test_plain_string_unchanged(self, dl):
        """Обычная строка без запрещённых символов остаётся нетронутой."""
        assert dl.clean_filename('Hello World') == 'Hello World'

    def test_colon_replaced(self, dl):
        """Двоеточие заменяется на подчёркивание."""
        assert dl.clean_filename('Hello: World') == 'Hello_ World'

    def test_slash_replaced(self, dl):
        """Прямой слеш заменяется на подчёркивание."""
        assert dl.clean_filename('Hello/World') == 'Hello_World'

    def test_backslash_replaced(self, dl):
        """Обратный слеш заменяется на подчёркивание."""
        assert dl.clean_filename('Hello\\World') == 'Hello_World'

    def test_double_quotes_replaced(self, dl):
        """Двойные кавычки заменяются на подчёркивание (через re.sub)."""
        assert dl.clean_filename('Hello"World"') == 'Hello_World_'

    def test_single_quotes_removed(self, dl):
        """Одинарные кавычки удаляются (replace → '')."""
        assert dl.clean_filename("Hello'World'") == 'HelloWorld'

    def test_en_dash_normalized(self, dl):
        """Длинное тире (–) заменяется на обычное тире (-)."""
        assert dl.clean_filename('Hello – World') == 'Hello - World'

    def test_em_dash_normalized(self, dl):
        """Очень длинное тире (—) заменяется на обычное тире (-)."""
        assert dl.clean_filename('Hello — World') == 'Hello - World'

    def test_ellipsis_normalized(self, dl):
        """Символ многоточия (…) заменяется на три точки (...)."""
        assert dl.clean_filename('Hello… World') == 'Hello... World'

    def test_multiple_spaces_collapsed(self, dl):
        """Несколько пробелов схлопываются в один."""
        assert dl.clean_filename('Hello   World') == 'Hello World'

    def test_leading_trailing_spaces_stripped(self, dl):
        """Ведущие и хвостовые пробелы обрезаются."""
        assert dl.clean_filename('  Hello World  ') == 'Hello World'

    def test_truncated_to_80_chars(self, dl):
        """Строка длиннее 80 символов усекается до 80."""
        result = dl.clean_filename('A' * 100)
        assert len(result) == 80

    def test_forbidden_chars_replaced(self, dl):
        """Символы <> заменяются на подчёркивание."""
        assert dl.clean_filename('Hello <World>') == 'Hello _World_'

    def test_question_mark_replaced(self, dl):
        """Вопросительный знак заменяется на подчёркивание."""
        assert dl.clean_filename('test?file') == 'test_file'

    def test_asterisk_replaced(self, dl):
        """Звёздочка заменяется на подчёркивание."""
        assert dl.clean_filename('test*file') == 'test_file'

    def test_pipe_replaced(self, dl):
        """Вертикальная черта заменяется на подчёркивание."""
        assert dl.clean_filename('test|file') == 'test_file'

    def test_dot_at_edges_stripped(self, dl):
        """Точки по краям строки обрезаются (strip(' .'))."""
        assert dl.clean_filename('.hidden.') == 'hidden'

    def test_empty_string(self, dl):
        """Пустая строка возвращается без изменений."""
        assert dl.clean_filename('') == ''


# ============================================================
# is_tracking_pixel
# ============================================================

class TestIsTrackingPixel:
    """Тесты определения трекинг-пикселей по домену и размеру."""

    def _img(self, html: str):
        """Вспомогательный метод: парсим <img> тег из HTML-строки."""
        return BeautifulSoup(html, 'html.parser').img

    def test_googletagmanager_domain(self, dl):
        """URL с доменом googletagmanager → True."""
        img = self._img('<img src="x.gif">')
        assert dl.is_tracking_pixel(img, 'https://www.googletagmanager.com/ns.html') is True

    def test_facebook_tr_domain(self, dl):
        """URL с facebook.com/tr → True."""
        img = self._img('<img src="x.gif">')
        assert dl.is_tracking_pixel(img, 'https://www.facebook.com/tr?id=123') is True

    def test_doubleclick_activity_domain(self, dl):
        """URL с doubleclick.net/activity → True."""
        img = self._img('<img src="x.gif">')
        assert dl.is_tracking_pixel(img, 'https://doubleclick.net/activity?foo=bar') is True

    def test_size_1x1(self, dl):
        """Изображение 1×1 → True (трекинг-пиксель по размеру)."""
        img = self._img('<img width="1" height="1" src="x.gif">')
        assert dl.is_tracking_pixel(img, 'https://example.com/pixel.gif') is True

    def test_size_2x2(self, dl):
        """Изображение 2×2 → True (w <= 2 или h <= 2)."""
        img = self._img('<img width="2" height="2" src="x.gif">')
        assert dl.is_tracking_pixel(img, 'https://example.com/pixel.gif') is True

    def test_size_1x3(self, dl):
        """Изображение 1×3 → True (w=1 <= 2)."""
        img = self._img('<img width="1" height="3" src="x.gif">')
        assert dl.is_tracking_pixel(img, 'https://example.com/pixel.gif') is True

    def test_size_3x1(self, dl):
        """Изображение 3×1 → True (h=1 <= 2)."""
        img = self._img('<img width="3" height="1" src="x.gif">')
        assert dl.is_tracking_pixel(img, 'https://example.com/pixel.gif') is True

    def test_size_3x3_normal(self, dl):
        """Изображение 3×3 — не пиксель, нормальный URL → False."""
        img = self._img('<img width="3" height="3" src="photo.jpg">')
        assert dl.is_tracking_pixel(img, 'https://example.com/photo.jpg') is False

    def test_large_image_not_pixel(self, dl):
        """Большое изображение 800×600 → False."""
        img = self._img('<img width="800" height="600" src="photo.jpg">')
        assert dl.is_tracking_pixel(img, 'https://example.com/photo.jpg') is False

    def test_no_dimensions_not_pixel(self, dl):
        """Изображение без атрибутов размера → False (если URL не трекинг)."""
        img = self._img('<img src="photo.jpg">')
        assert dl.is_tracking_pixel(img, 'https://example.com/photo.jpg') is False

    def test_only_width_no_height_not_pixel(self, dl):
        """Только width без height — условие (width AND height) не выполняется → False."""
        img = self._img('<img width="1" src="photo.jpg">')
        assert dl.is_tracking_pixel(img, 'https://example.com/photo.jpg') is False
