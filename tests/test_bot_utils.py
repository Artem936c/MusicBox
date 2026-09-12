"""Тесты рендера списков бота: экранирование заголовков и строк треков.

Все сообщения бота уходят с ``parse_mode=HTML``, поэтому текст, собранный
`render_track_list`, обязан быть разбираемым Telegram: разметкой остаются только
известные парные теги без атрибутов, а «<», «>» и «&» из пользовательских данных
превращаются в сущности. Проверяет это `assert_telegram_html`.
"""

from __future__ import annotations

import logging
import re

import pytest

from backend.bot import utils

logger = logging.getLogger(__name__)

#: Теги, которые Telegram понимает в режиме HTML (`a` — только со ссылкой).
TELEGRAM_TAGS = frozenset(
    {
        "b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
        "code", "pre", "blockquote", "tg-spoiler", "span", "a",
    }
)

#: Тег с необязательными атрибутами.
_TAG_RE = re.compile(r"</?(?P<name>[a-zA-Z][a-zA-Z0-9-]*)(?P<attrs>\s[^<>]*)?>")
#: Корректная HTML-сущность: именованная, десятичная или шестнадцатеричная.
_ENTITY_RE = re.compile(r"&(?:#\d+|#[xX][0-9a-fA-F]+|[a-zA-Z][a-zA-Z0-9]*);")


def assert_telegram_html(text: str) -> None:
    """Проверяет, что текст безопасен для отправки с `parse_mode=HTML`.

    Требования те же, что предъявляет Telegram: неизвестных тегов нет, парные
    теги закрыты в правильном порядке, а вне тегов не остаётся «сырых» «<», «>»
    и одиночных «&».
    """
    stack: list[str] = []
    position = 0

    for match in _TAG_RE.finditer(text):
        _assert_plain_text(text[position : match.start()], text)
        position = match.end()
        name = match.group("name").casefold()
        assert name in TELEGRAM_TAGS, f"Telegram не знает тег <{name}> в тексте: {text!r}"
        if match.group(0).startswith("</"):
            assert stack and stack[-1] == name, f"Тег </{name}> закрыт не вовремя: {text!r}"
            stack.pop()
        else:
            stack.append(name)

    _assert_plain_text(text[position:], text)
    assert not stack, f"Не закрыты теги {stack} в тексте: {text!r}"


def _assert_plain_text(chunk: str, full_text: str) -> None:
    """Кусок текста между тегами: без угловых скобок и без «голых» амперсандов."""
    assert "<" not in chunk, f"Сырая «<» вне тега в тексте: {full_text!r}"
    assert ">" not in chunk, f"Сырая «>» вне тега в тексте: {full_text!r}"
    rest = _ENTITY_RE.sub("", chunk)
    assert "&" not in rest, f"«&» без сущности в тексте: {full_text!r}"


#: Один трек с «опасными» названием и исполнителем.
DANGEROUS_TRACK: dict = {
    "title": "R&B <b>hit</b>",
    "artist": "AC/DC & <script>",
    "duration": 187,
    "play_count": 3,
    "is_favourite": True,
}

EMPTY_TEXT = "🎵 Здесь пока пусто."


# ---------------------------------------------------------------------------
# Проверка самого валидатора
# ---------------------------------------------------------------------------


def test_validator_rejects_raw_markup() -> None:
    """Валидатор ловит сырые угловые скобки, чужие теги и незакрытую разметку."""
    with pytest.raises(AssertionError):
        assert_telegram_html("Папка <script>alert(1)</script>")
    with pytest.raises(AssertionError):
        assert_telegram_html("Rock & Roll")
    with pytest.raises(AssertionError):
        assert_telegram_html("<b>не закрыт")


def test_validator_accepts_correct_markup() -> None:
    """Правильно экранированный текст валидатор пропускает."""
    assert_telegram_html("<b>Rock &amp; Roll</b> &lt;не тег&gt;")


# ---------------------------------------------------------------------------
# render_track_list: заголовок
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "Папка <script>alert(1)</script>",
        "Rock & Roll",
        "AC/DC & <b>лучшее</b>",
        "1 < 2 & 3 > 2",
        "<b>незакрытый жирный",
        "</b>",
        '<a href="https://example.com">ссылка</a>',
        "<img src=x onerror=alert(1)>",
    ],
)
def test_render_track_list_header_is_html_safe(title: str) -> None:
    """Любой заголовок остаётся разбираемым HTML и не отдаёт сырые «<» и «&»."""
    text = utils.render_track_list(title, [DANGEROUS_TRACK], 1, 1, EMPTY_TEXT)

    assert_telegram_html(text)


def test_render_track_list_escapes_angle_brackets_and_ampersand() -> None:
    """Заголовок с «<» и «&» уходит сущностями, а не разметкой."""
    text = utils.render_track_list(
        "Папка <script> & Ко", [DANGEROUS_TRACK], 1, 1, EMPTY_TEXT
    )

    assert_telegram_html(text)
    assert "<script>" not in text
    assert "&lt;script&gt;" in text
    assert "&amp; Ко" in text
    # Содержимое заголовка не потерялось, а только экранировалось.
    assert "Папка" in text


def test_render_track_list_keeps_own_markup() -> None:
    """Собственная разметка заголовка (`<b>`) сохраняется, а «&» экранируется."""
    text = utils.render_track_list("<b>Кино</b> & Ко", [], 1, 1, EMPTY_TEXT)

    assert_telegram_html(text)
    assert text.startswith("<b>Кино</b> &amp; Ко")


def test_render_track_list_does_not_double_escape() -> None:
    """Уже экранированный заголовок не превращается в «&amp;lt;»."""
    text = utils.render_track_list("&lt;Кино&gt; &amp; Ко", [], 1, 1, EMPTY_TEXT)

    assert_telegram_html(text)
    assert "&amp;lt;" not in text
    assert "&lt;Кино&gt; &amp; Ко" in text


def test_render_track_list_bolds_plain_header() -> None:
    """Заголовок без собственной разметки выделяется жирным."""
    text = utils.render_track_list("Мои треки", [], 1, 1, EMPTY_TEXT)

    assert_telegram_html(text)
    assert text.startswith("<b>Мои треки</b>")


# ---------------------------------------------------------------------------
# render_track_list: строки треков и подвал
# ---------------------------------------------------------------------------


def test_render_track_list_escapes_track_fields() -> None:
    """Название и исполнитель трека экранируются, разметка не ломается."""
    text = utils.render_track_list("Список", [DANGEROUS_TRACK], 1, 1, EMPTY_TEXT)

    assert_telegram_html(text)
    assert "<b>R&amp;B &lt;b&gt;hit&lt;/b&gt;</b>" in text
    assert "AC/DC &amp; &lt;script&gt;" in text


def test_render_track_list_paginated_footer_is_safe() -> None:
    """Подвал с номером страницы не ломает разметку опасного заголовка."""
    text = utils.render_track_list("Rock & <i>Roll", [DANGEROUS_TRACK], 2, 3, EMPTY_TEXT)

    assert_telegram_html(text)
    assert text.endswith("</i>")


def test_render_track_list_empty_keeps_ready_text() -> None:
    """Пустой список показывает готовый текст без искажений."""
    text = utils.render_track_list("Rock & Roll", [], 1, 1, EMPTY_TEXT)

    assert_telegram_html(text)
    assert text.endswith(EMPTY_TEXT)


def test_escape_leaves_no_raw_specials() -> None:
    """`escape` закрывает все три опасных символа и переваривает None."""
    assert utils.escape("<b>&</b>") == "&lt;b&gt;&amp;&lt;/b&gt;"
    assert utils.escape(None) == ""
