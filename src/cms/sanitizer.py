"""CMS Markdown → sanitized HTML (NEWS-1 #341, общий с HC #332).

Контракт безопасности:
  • markdown-it-py в режиме commonmark с ВЫКЛЮЧЕННЫМ raw-HTML — авторский
    <script> и любой встроенный HTML не парсится как разметка;
  • поверх — nh3 (Rust ammonia): строгий allowlist тегов/атрибутов,
    ссылки только http/https/mailto (javascript: вырезается), никаких
    style/svg/iframe/img-onerror;
  • body-size limit до рендера — DoS-гейт (операторский ввод, но админ
    тоже может вставить мегабайты случайно).

Картинки разрешены ТОЛЬКО с относительным /cms/media/... src (HC-3
медиа-пайплайн) или https — data:-URI вырезается nh3 по умолчанию.
"""
from __future__ import annotations

import nh3
from markdown_it import MarkdownIt

# 256 KiB Markdown — с запасом для длинной статьи, но не для DoS
BODY_MD_MAX_BYTES = 256 * 1024

_ALLOWED_TAGS = {
    "a", "abbr", "blockquote", "br", "code", "em", "h1", "h2", "h3", "h4",
    "hr", "img", "li", "ol", "p", "pre", "strong", "table", "tbody", "td",
    "th", "thead", "tr", "ul", "del", "sup", "sub",
}
_ALLOWED_ATTRS = {
    "a": {"href", "title"},
    "img": {"src", "alt", "title"},
    "td": {"align"},
    "th": {"align"},
}

# commonmark: raw HTML выключен по умолчанию; включаем только таблицы и
# зачёркивание — без html:true НИКОГДА.
_md = MarkdownIt("commonmark", {"html": False, "linkify": False}) \
    .enable("table") \
    .enable("strikethrough")


def render_markdown(body_md: str) -> str:
    """Санированный HTML для body_html-кеша. ValueError на превышение
    лимита — вызывающий отдаёт 422, а не молча режет контент."""
    raw = body_md.encode("utf-8")
    if len(raw) > BODY_MD_MAX_BYTES:
        raise ValueError(
            f"body_md too large: {len(raw)} bytes > {BODY_MD_MAX_BYTES}"
        )
    html = _md.render(body_md)
    return nh3.clean(
        html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRS,
        url_schemes={"http", "https", "mailto"},
        link_rel="noopener noreferrer",
    )
