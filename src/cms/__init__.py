"""src/cms — общий CMS-фундамент «Новостей» (#340) и Help Center (#332).

Единственное место, где авторский Markdown превращается в HTML:
markdown-it-py (commonmark, БЕЗ raw-HTML) → nh3-санитайзер со строгим
allowlist. Оба контент-типа обязаны ходить через render_markdown() —
дублирование санитайзера = два места для XSS-дыры.
"""
from src.cms.sanitizer import BODY_MD_MAX_BYTES, render_markdown

__all__ = ["render_markdown", "BODY_MD_MAX_BYTES"]
