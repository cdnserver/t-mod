"""Pure market identities, normalization and search ranking."""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import hashlib
import re
import unicodedata
from typing import Any, Iterable

from modules.market_config import (
    MARKET_CATEGORY,
    MARKET_CATEGORY_INFO,
    MARKET_SEARCH_LIMIT,
)


def normalize_market_text(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().replace("ё", "е")
    text = re.sub(r"[^0-9a-zа-я]+", " ", text, flags=re.IGNORECASE)
    return " ".join(text.split())


def transliterate_ru(value: str) -> str:
    mapping = {
        "а": "a",
        "б": "b",
        "в": "v",
        "г": "g",
        "д": "d",
        "е": "e",
        "ё": "e",
        "ж": "zh",
        "з": "z",
        "и": "i",
        "й": "i",
        "к": "k",
        "л": "l",
        "м": "m",
        "н": "n",
        "о": "o",
        "п": "p",
        "р": "r",
        "с": "s",
        "т": "t",
        "у": "u",
        "ф": "f",
        "х": "h",
        "ц": "ts",
        "ч": "ch",
        "ш": "sh",
        "щ": "sch",
        "ъ": "",
        "ы": "y",
        "ь": "",
        "э": "e",
        "ю": "yu",
        "я": "ya",
    }
    return "".join(mapping.get(char, char) for char in normalize_market_text(value))


def text_match_score(query: str, name: str) -> int:
    if not query or not name:
        return 0
    if query == name:
        return 15_000
    if name.startswith(query):
        return 12_000 - min(1000, len(name) - len(query))
    if query in name:
        return 10_000 - min(1000, name.index(query) * 10)

    query_tokens = query.split()
    name_tokens = name.split()
    if query_tokens and all(
        any(token.startswith(part) or part.startswith(token) for token in name_tokens)
        for part in query_tokens
    ):
        return 9000 + sum(min(len(part), 20) for part in query_tokens)

    whole_ratio = SequenceMatcher(None, query, name).ratio()
    token_ratio = max(
        (SequenceMatcher(None, part, token).ratio() for part in query_tokens for token in name_tokens),
        default=0.0,
    )
    ratio = max(whole_ratio, token_ratio * 0.92)
    return int(ratio * 8000) if ratio >= 0.52 else 0


def market_search_score(query: str, item: dict[str, Any]) -> int:
    category = str(item.get("category") or MARKET_CATEGORY)
    external_id = normalize_market_text(item.get("external_id") or item.get("item_id") or "")
    raw_internal_id = str(item.get("item_id") or "")
    if category == "items" and query.lstrip("#") == raw_internal_id and (query.startswith("#") or query.isdigit()):
        return 20_000
    if query == external_id:
        return 18_000
    name = str(item.get("normalized_name") or normalize_market_text(item.get("item_name") or ""))
    queries = {query, transliterate_ru(query)}
    names = {name, external_id, transliterate_ru(name)}
    return max(
        (text_match_score(candidate_query, candidate_name) for candidate_query in queries for candidate_name in names),
        default=0,
    )


def market_internal_id(category: str, external_id: str) -> int:
    clean_category = str(category).strip().lower()
    clean_external_id = str(external_id).strip()
    if clean_category == "items" and clean_external_id.isdigit():
        return int(clean_external_id)
    digest = hashlib.blake2b(
        f"{clean_category}:{clean_external_id}".encode("utf-8"),
        digest_size=8,
        person=b"tmodmkt",
    ).digest()
    return int.from_bytes(digest, "big") & ((1 << 63) - 1)


def clean_market_category(category: str | None) -> str:
    clean = str(category or MARKET_CATEGORY).strip().lower()
    return clean if clean in MARKET_CATEGORY_INFO else MARKET_CATEGORY


def category_info(category: str) -> dict[str, str]:
    return MARKET_CATEGORY_INFO[clean_market_category(category)]


def item_identity(item: dict[str, Any]) -> str:
    category = clean_market_category(item.get("category"))
    external_id = str(item.get("external_id") or item.get("item_id") or "")
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    if category == "items":
        return f"#{external_id}"
    if category == "vehicles":
        return f"model: {external_id}"
    gender = "муж." if int(metadata.get("gender") or 0) == 0 else "жен."
    kind = "аксессуар" if int(metadata.get("isProp") or 0) else "одежда"
    return (
        f"{gender} · {kind} · c{int(metadata.get('component') or 0)} / "
        f"d{int(metadata.get('drawable') or 0)} / t{int(metadata.get('texture') or 0)}"
    )


def quantity_label(category: str) -> str:
    return category_info(category)["quantity"]


def quantity_value(item: dict[str, Any]) -> int:
    category = clean_market_category(item.get("category"))
    if category == "clothes":
        return int(item.get("sold_count") or 0)
    return int(item.get("total_count") or 0)


@dataclass(frozen=True, slots=True)
class MarketSearchHit:
    item: dict[str, Any]
    score: int


@dataclass(frozen=True, slots=True)
class MarketSnapshotChange:
    category: str
    source_updated_at: str
    record_count: int


def rank_market_items(
    items: Iterable[dict[str, Any]],
    query: str,
    limit: int = MARKET_SEARCH_LIMIT,
) -> list[MarketSearchHit]:
    clean_query = normalize_market_text(query)
    if not clean_query:
        return []
    hits = [
        MarketSearchHit(item=item, score=score)
        for item in items
        if (score := market_search_score(clean_query, item)) > 0
    ]
    hits.sort(
        key=lambda hit: (
            -hit.score,
            -int(hit.item.get("sold_count") or 0),
            str(hit.item.get("item_name") or "").casefold(),
        )
    )
    return hits[: max(1, min(int(limit), MARKET_SEARCH_LIMIT))]


# Compatibility aliases used by the legacy facade while the migration is active.
_transliterate_ru = transliterate_ru
_text_match_score = text_match_score
_market_search_score = market_search_score
_market_internal_id = market_internal_id
_clean_market_category = clean_market_category
_category_info = category_info
_item_identity = item_identity
_quantity_label = quantity_label
_quantity_value = quantity_value
