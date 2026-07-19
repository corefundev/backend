"""
src/validation/column_mapping.py  —  DP-3 (#323)

Column-mapping engine for «Подготовка данных». Takes the header row from a sniff
report (DP-2) and proposes how the user's columns map onto our canonical schema
(date, sku, sales + optional price, promo, stock).

This runs OUTSIDE the sandbox — it operates only on header STRINGS already
extracted (and formula-sanitised) by the in-sandbox sniff, never on raw file
bytes. The parser applies a CONFIRMED mapping in-sandbox (secure_parser
--mapping); proposing here keeps the heavy dictionary out of the hardened image.

Matching ladder (best score wins per field):
    1. exact       — normalised header == a known synonym            (1.00)
    2. token/substr— synonym is a whole token of the header, or the
                     header starts with the synonym                   (0.88)
    3. fuzzy       — bounded in-house Levenshtein ratio ≥ 0.85        (= ratio)

No third-party fuzzy libs (rapidfuzz/fuzzywuzzy) — supply-chain policy. The
Levenshtein is a small, bounded DP with an early length-gap reject.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# ── Canonical schema ──────────────────────────────────────────────────────────
REQUIRED_FIELDS = ("date", "sku", "sales")
OPTIONAL_FIELDS = ("price", "promo", "stock", "category")
CANONICAL_FIELDS = REQUIRED_FIELDS + OPTIONAL_FIELDS


# ── Synonym dictionary ────────────────────────────────────────────────────────
# Extensive on purpose (#323): real column names from 1С, Wildberries, Ozon,
# generic ERP and hand-made Excel, RU + EN. Entries are matched AFTER _normalize
# (lowercase, ё→е, punctuation→space, collapsed spaces, кол-во→количество), so
# list them in that normalised form. Order is irrelevant; duplicates are fine.
_SYNONYMS: Dict[str, List[str]] = {
    "date": [
        # RU
        "дата", "дата продажи", "дата продаж", "дата отгрузки", "дата документа",
        "дата реализации", "дата заказа", "дата операции", "дата чека",
        "дата продажи товара", "дата отгр", "период", "период продаж", "день",
        "число", "месяц", "неделя", "отчетный период", "отчетная дата",
        "календарный день", "дата факт", "дата фактическая",
        # EN
        "date", "day", "date of sale", "sale date", "sales date", "sell date",
        "order date", "ship date", "shipment date", "shipping date",
        "transaction date", "invoice date", "posting date", "document date",
        "delivery date", "period", "week", "month", "timestamp", "datetime",
        "dt", "order dt", "calendar day", "reporting date", "report date",
    ],
    "sku": [
        # RU
        "артикул", "артикул товара", "артикул поставщика", "артикул продавца",
        "артикул wb", "артикул wildberries", "артикул озон", "номенклатура",
        "номенклатура код", "код номенклатуры", "код товара", "код", "код sku",
        "товар", "товары", "наименование", "наименование товара",
        "наименование позиции", "наименование номенклатуры", "продукт",
        "позиция", "товарная позиция", "штрихкод", "штрих код", "sku",
        "ску", "код продукта", "модель", "модель товара",
        # marketplace ids
        "offer id", "оффер id", "sku id", "ozon id", "wb id", "id товара",
        "идентификатор товара", "баркод",
        # EN
        "sku", "item", "item id", "item code", "item number", "item no",
        "product", "product id", "product code", "product name", "article",
        "article number", "article no", "material", "material number",
        "material code", "part number", "part no", "part", "code", "name",
        "product sku", "stock keeping unit", "nomenclature", "upc", "ean",
        "ean13", "gtin", "barcode", "bar code", "model", "style", "style code",
    ],
    "sales": [
        # RU
        "продажи", "продажа", "продано", "продано штук", "продажи шт",
        "продажи штук", "продано шт", "количество", "количество продаж",
        "кол во", "кол во продаж", "колво", "объем", "объем продаж",
        "объём продаж", "отгрузка", "отгружено", "отгрузка шт", "реализация",
        "реализовано", "спрос", "штук", "шт", "штуки", "единиц", "ед",
        "факт", "факт продаж", "фактические продажи", "заказано",
        "заказано штук", "заказы", "количество заказов", "проданное количество",
        "количество товара", "расход", "выбытие",
        # EN
        "sales", "sale", "sold", "units", "units sold", "unit sales",
        "qty", "qty sold", "quantity", "quantity sold", "sales qty",
        "sales quantity", "sales units", "volume", "sales volume", "demand",
        "count", "pieces", "pcs", "ordered", "order qty", "ordered units",
        "shipped", "shipped qty", "outbound",
    ],
    "price": [
        # RU
        "цена", "цена продажи", "цена реализации", "цена за единицу",
        "цена за шт", "цена за штуку", "цена единицы", "розничная цена",
        "отпускная цена", "средняя цена", "цена руб", "цена рублей",
        "стоимость", "стоимость единицы", "стоимость товара", "прайс",
        "цена продавца", "цена до скидки", "цена со скидкой", "закупочная цена",
        "себестоимость",
        # EN
        "price", "unit price", "sale price", "selling price", "sell price",
        "retail price", "list price", "price per unit", "price per item",
        "unit cost", "cost", "cost price", "avg price", "average price",
        "msrp", "rate", "amount per unit", "price rub",
    ],
    "promo": [
        # RU
        "промо", "промоакция", "промо акция", "акция", "акции", "скидка",
        "распродажа", "участие в акции", "флаг акции", "признак акции",
        "маркетинговая акция", "спеццена", "специальная цена", "есть скидка",
        "промо флаг", "флаг промо", "акционный", "уценка",
        # EN
        "promo", "promotion", "promo flag", "is promo", "on promo", "discount",
        "discounted", "sale flag", "on sale", "deal", "markdown", "campaign",
        "promotion flag", "special offer", "special price", "clearance",
    ],
    "stock": [
        # RU
        "остаток", "остатки", "остаток на складе", "остаток шт",
        "товарный остаток", "конечный остаток", "доступный остаток",
        "склад", "на складе", "количество на складе", "кол во на складе",
        "запас", "запасы", "наличие", "в наличии", "доступно",
        "доступно к продаже", "остаток wb", "остаток озон", "fbo остаток",
        "fbs остаток", "остаток fbo", "остаток fbs",
        # EN
        "stock", "stock qty", "stock quantity", "stock level", "in stock",
        "inventory", "inventory qty", "inventory level", "on hand",
        "quantity on hand", "qoh", "available", "available qty",
        "available to sell", "warehouse", "warehouse qty", "balance",
        "ending stock", "ending inventory", "remaining", "on hand qty",
    ],
    # #545: категория SKU → category_te (третья static-фича). 1С обычно
    # несёт её как «группа»/«номенклатурная группа»; маркетплейсы — как
    # «предмет»/«категория товара».
    "category": [
        # RU
        "категория", "категория товара", "категория номенклатуры",
        "группа", "группа товара", "группа товаров", "товарная группа",
        "номенклатурная группа", "подгруппа", "раздел", "рубрика",
        "предмет", "тип товара", "вид товара", "вид номенклатуры",
        "направление", "сегмент", "коллекция", "линейка",
        # EN
        "category", "product category", "item category", "category name",
        "group", "product group", "item group", "commodity group",
        "department", "section", "class", "product class", "subcategory",
        "sub category", "product type", "item type", "segment",
        "collection", "product line", "family", "product family",
    ],
}

# Confidence bands for the best match score.
_HIGH, _MEDIUM = 0.90, 0.75
# Fuzzy acceptance floor and a length-gap guard for the Levenshtein.
_FUZZY_FLOOR = 0.85
_TOKEN_SCORE = 0.88

_PUNCT_RE = re.compile(r"[._/\\|()\[\]{}№#,:;+\-]+")
_WS_RE = re.compile(r"\s+")


def _normalize(header: str) -> str:
    """Canonicalise a header for matching: lowercase, ё→е, punctuation→space,
    collapse whitespace, expand the ubiquitous 'кол-во' abbreviation."""
    s = str(header).strip().lower().replace("ё", "е")
    s = _PUNCT_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    s = re.sub(r"\bкол во\b", "количество", s)
    return s


def _levenshtein_ratio(a: str, b: str) -> float:
    """Similarity in [0,1] = 1 - dist/max(len). Bounded: bail early when the
    length gap alone already puts the ratio under the fuzzy floor."""
    la, lb = len(a), len(b)
    if not la or not lb:
        return 0.0
    if abs(la - lb) / max(la, lb) > (1.0 - _FUZZY_FLOOR):
        return 0.0
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        ca = a[i - 1]
        for j in range(1, lb + 1):
            cost = 0 if ca == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        prev = cur
    dist = prev[lb]
    return 1.0 - dist / max(la, lb)


def _score_header(norm_header: str, synonyms: List[str]) -> float:
    """Best match score of one normalised header against one field's synonyms."""
    if not norm_header:
        return 0.0
    htokens = norm_header.split()
    best = 0.0
    for syn in synonyms:
        if norm_header == syn:
            return 1.0
        # whole-token containment or a clean prefix ("артикул поставщика")
        stokens = syn.split()
        if len(stokens) == 1:
            if syn in htokens:
                best = max(best, _TOKEN_SCORE)
            elif norm_header.startswith(syn + " ") or norm_header.endswith(" " + syn):
                best = max(best, _TOKEN_SCORE)
        elif syn in norm_header:
            best = max(best, _TOKEN_SCORE)
        # bounded fuzzy for typos / minor tails
        if best < 1.0:
            r = _levenshtein_ratio(norm_header, syn)
            if r >= _FUZZY_FLOOR:
                best = max(best, r)
    return best


def _band(score: float) -> str:
    if score >= _HIGH:
        return "high"
    if score >= _MEDIUM:
        return "medium"
    return "low"


@dataclass
class MappingProposal:
    """Result of propose_mapping. `mapping` gives canonical→source-header (None
    when unmatched); `field_confidence` is the band per canonical field."""
    mapping: Dict[str, Optional[str]]
    field_confidence: Dict[str, str]
    field_score: Dict[str, float]
    unmapped_headers: List[str] = field(default_factory=list)
    missing_required: List[str] = field(default_factory=list)
    auto_confirmable: bool = False

    def to_dict(self) -> dict:
        return {
            "mapping": self.mapping,
            "field_confidence": self.field_confidence,
            "field_score": {k: round(v, 3) for k, v in self.field_score.items()},
            "unmapped_headers": self.unmapped_headers,
            "missing_required": self.missing_required,
            "auto_confirmable": self.auto_confirmable,
        }


def propose_mapping(headers: List[str]) -> MappingProposal:
    """Propose a canonical mapping for the given header row.

    Each canonical field is assigned its best-scoring header; a header is used
    for at most one field (highest score wins, resolved greedily). REQUIRED
    fields with no match land in `missing_required` (the prep worker fails the
    upload with a human hint in that case). `auto_confirmable` — every REQUIRED
    field matched at HIGH confidence — is retained as informational metadata on
    the stored proposal (the system auto-maps regardless; there is no user
    confirmation step), useful for observability of low-confidence auto-maps.
    """
    norm = {h: _normalize(h) for h in headers}
    # score[field][header] = match score
    scored: List[tuple] = []   # (score, field, header)
    for fld in CANONICAL_FIELDS:
        syns = _SYNONYMS[fld]
        for h in headers:
            s = _score_header(norm[h], syns)
            if s > 0.0:
                scored.append((s, fld, h))
    # greedy assignment: highest score first, one header per field, one field
    # per header.
    scored.sort(key=lambda t: t[0], reverse=True)
    mapping: Dict[str, Optional[str]] = {f: None for f in CANONICAL_FIELDS}
    field_score: Dict[str, float] = {f: 0.0 for f in CANONICAL_FIELDS}
    used_headers: set = set()
    for s, fld, h in scored:
        if mapping[fld] is not None or h in used_headers:
            continue
        mapping[fld] = h
        field_score[fld] = s
        used_headers.add(h)

    field_confidence = {
        f: (_band(field_score[f]) if mapping[f] is not None else "none")
        for f in CANONICAL_FIELDS
    }
    unmapped = [h for h in headers if h not in used_headers]
    missing_required = [f for f in REQUIRED_FIELDS if mapping[f] is None]
    auto_confirmable = (
        not missing_required
        and all(field_confidence[f] == "high" for f in REQUIRED_FIELDS)
    )
    return MappingProposal(
        mapping=mapping,
        field_confidence=field_confidence,
        field_score=field_score,
        unmapped_headers=unmapped,
        missing_required=missing_required,
        auto_confirmable=auto_confirmable,
    )
