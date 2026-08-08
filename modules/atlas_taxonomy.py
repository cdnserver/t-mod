"""Deterministic Atlas knowledge taxonomy shared by ingestion and retrieval."""

from __future__ import annotations

import re
from typing import Any


ATLAS_KNOWLEDGE_DOMAINS = (
    {"code": "ic", "label": "IC · игровой мир"},
    {"code": "ooc", "label": "OOC · правила проекта"},
    {"code": "mixed", "label": "Смешанный материал"},
)
ATLAS_CORPUS_KINDS = (
    {"code": "law", "label": "Законодательство"},
    {"code": "server_rule", "label": "Правила сервера"},
    {"code": "charter", "label": "Устав организации"},
    {"code": "case_law", "label": "Судебная практика"},
    {"code": "lawsuit", "label": "Иск / судебный материал"},
    {"code": "department_order", "label": "Приказ / распоряжение"},
    {"code": "procedure", "label": "Процедура / регламент"},
    {"code": "manual", "label": "Памятка / обучение"},
    {"code": "forum", "label": "Материал форума"},
    {"code": "other", "label": "Другой материал"},
)

_DOMAIN_CODES = frozenset(item["code"] for item in ATLAS_KNOWLEDGE_DOMAINS)
_CORPUS_CODES = frozenset(item["code"] for item in ATLAS_CORPUS_KINDS)
_OOC_RE = re.compile(
    r"\b(?:ooc|оо[сc]|правил[ао]\s+(?:сервера|проекта|форума)|наказани[ея]\s+администрац|"
    r"discord|дискорд|мультиаккаунт|метагейм|powergaming|dm|db|rk|тк|пг|мг)\b",
    re.IGNORECASE,
)
_IC_RE = re.compile(
    r"\b(?:ic|и[сc]|закон|кодекс|конституц|устав|постановлен|приказ|распоряжен|"
    r"судеб|иск(?:овое|а|и)?|прокуратур|адвокат|задержан|арест|обыск|полномочи)\w*",
    re.IGNORECASE,
)
_CORPUS_PATTERNS = (
    ("server_rule", re.compile(r"правил[ао]\s+(?:сервера|проекта|форума)|ooc|оо[сc]", re.I)),
    ("case_law", re.compile(r"судебн\w*\s+(?:практик|решен|прецедент)|решение\s+суда", re.I)),
    ("lawsuit", re.compile(r"\bиск(?:овое\s+заявление|а|и)?\b|ходатайств|материал[ыа]\s+дела", re.I)),
    ("charter", re.compile(r"\bустав\w*|положени[ея]\s+(?:об|о)\s+(?:организац|департамент)", re.I)),
    ("department_order", re.compile(r"\bприказ\w*|распоряжен\w*|директив\w*", re.I)),
    ("law", re.compile(r"\bзакон\w*|кодекс\w*|конституц\w*|нормативн\w*\s+акт", re.I)),
    ("procedure", re.compile(r"регламент\w*|процедур\w*|порядок\s+(?:проведен|действ)", re.I)),
    ("manual", re.compile(r"памятк\w*|обучени\w*|инструкци\w*|руководств\w*", re.I)),
)


def atlas_normalize_knowledge_domain(value: str | None) -> str | None:
    selected = str(value or "").strip().lower()
    return selected if selected in _DOMAIN_CODES else None


def atlas_normalize_corpus_kind(value: str | None) -> str | None:
    selected = str(value or "").strip().lower()
    return selected if selected in _CORPUS_CODES else None


def atlas_classify_knowledge(
    *,
    title: str,
    content: str,
    source_url: str | None = None,
    source_kind: str | None = None,
    domain_hint: str | None = None,
    corpus_hint: str | None = None,
) -> dict[str, Any]:
    """Classify without an LLM so ingestion remains fast, private and repeatable."""

    sample = "\n".join((str(title or ""), str(source_url or ""), str(content or "")[:12_000]))
    hinted_domain = atlas_normalize_knowledge_domain(domain_hint)
    hinted_corpus = atlas_normalize_corpus_kind(corpus_hint)
    has_ooc = bool(_OOC_RE.search(sample))
    has_ic = bool(_IC_RE.search(sample))
    domain = hinted_domain or ("mixed" if has_ooc == has_ic else "ooc" if has_ooc else "ic")

    corpus = hinted_corpus
    if corpus is None:
        corpus = next((code for code, pattern in _CORPUS_PATTERNS if pattern.search(sample)), None)
    if corpus is None:
        corpus = {
            "regulation": "procedure",
            "manual": "manual",
            "forum": "forum",
        }.get(str(source_kind or "").lower(), "other")

    authority_scope = (
        "project"
        if domain == "ooc"
        else "court"
        if corpus in {"case_law", "lawsuit"}
        else "organization"
        if corpus in {"charter", "department_order"}
        else "state"
        if corpus == "law"
        else "operational"
    )
    return {
        "domain": domain,
        "corpus_kind": corpus,
        "authority_scope": authority_scope,
        "classifier": "atlas-taxonomy-v1",
        "confidence": "explicit" if hinted_domain or hinted_corpus else "heuristic",
    }


def atlas_taxonomy_catalog() -> dict[str, list[dict[str, str]]]:
    return {
        "knowledge_domains": [dict(item) for item in ATLAS_KNOWLEDGE_DOMAINS],
        "corpus_kinds": [dict(item) for item in ATLAS_CORPUS_KINDS],
    }


__all__ = [
    "ATLAS_CORPUS_KINDS",
    "ATLAS_KNOWLEDGE_DOMAINS",
    "atlas_classify_knowledge",
    "atlas_normalize_corpus_kind",
    "atlas_normalize_knowledge_domain",
    "atlas_taxonomy_catalog",
]
