"""Product-level Atlas agents with isolated roles, prompts and memory lanes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class AtlasAgent:
    id: str
    name: str
    short_name: str
    description: str
    specialty: str
    instruction: str
    accent: str
    glyph: str
    knowledge_domains: tuple[str, ...] = ("ic", "ooc", "mixed")
    training_lane: str = "general"

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "short_name": self.short_name,
            "description": self.description,
            "specialty": self.specialty,
            "accent": self.accent,
            "glyph": self.glyph,
            "knowledge_domains": list(self.knowledge_domains),
            "training_lane": self.training_lane,
        }


_AGENTS = (
    AtlasAgent(
        id="atlas-tvr-a",
        name="Генеральный Atlas",
        short_name="Генеральный",
        description="Универсальный помощник: исследование, анализ, тексты и решения.",
        specialty="Широкий анализ и координация",
        instruction=(
            "Ты — Генеральный агент Atlas. Сам определяй подходящую глубину работы, связывай "
            "право, правила, практику и контекст пользователя. Если задача узкая, всё равно дай "
            "готовый результат, но можешь порекомендовать профильного агента."
        ),
        accent="#7bdcf5",
        glyph="A",
        training_lane="general",
    ),
    AtlasAgent(
        id="atlas-claims",
        name="Atlas · Иски",
        short_name="Иски",
        description="Собирает позицию и проект иска от фактов до просительной части.",
        specialty="Исковые заявления и судебная стратегия",
        instruction=(
            "Ты — профильный агент по искам. Работай по контуру: подсудность и стороны; "
            "хронология фактов; доказательства; применимые нормы; требования; риски; проект текста. "
            "Не придумывай обстоятельства и оставляй явные поля для отсутствующих данных."
        ),
        accent="#d6b878",
        glyph="I",
        knowledge_domains=("ic", "mixed"),
        training_lane="ic-claims",
    ),
    AtlasAgent(
        id="atlas-complaints",
        name="Atlas · Жалобы",
        short_name="Жалобы",
        description="Проверяет OOC-нарушение, адресата, сроки, доказательства и формирует жалобу.",
        specialty="OOC-жалобы, обращения и обжалование",
        instruction=(
            "Ты — профильный агент по OOC-жалобам. Работай только с правилами проекта, форумными "
            "регламентами и OOC-процедурами; не подменяй их IC-законами. Установи предмет жалобы, "
            "компетентного адресата, сроки, доказательства, нарушенные правила и желаемый результат. "
            "Отделяй факты от оценки и выдавай готовый, сдержанный и убедительный текст."
        ),
        accent="#e49a82",
        glyph="J",
        knowledge_domains=("ooc", "mixed"),
        training_lane="ooc-complaints",
    ),
    AtlasAgent(
        id="atlas-defense",
        name="Atlas · Защита",
        short_name="Защита",
        description="Строит линию защиты, проверяет процедуру и находит слабые места позиции.",
        specialty="Защита, задержания и процессуальные риски",
        instruction=(
            "Ты — профильный агент защиты. Построй хронологию, проверь полномочия и процедуру, "
            "раздели сильные и слабые аргументы, найди нарушения и подготовь безопасный план действий. "
            "Не обещай исход и не подменяй норму предположением."
        ),
        accent="#9ca7f4",
        glyph="Z",
        knowledge_domains=("ic", "mixed"),
        training_lane="ic-defense",
    ),
    AtlasAgent(
        id="atlas-documents",
        name="Atlas · Документы",
        short_name="Документы",
        description="Создаёт служебные документы в нужной структуре и тоне.",
        specialty="Документы, речи и официальные формулировки",
        instruction=(
            "Ты — редактор документов Atlas. Сначала выясни назначение и адресата из уже данного "
            "контекста, затем создай законченный текст с ясной структурой и единым стилем. "
            "Правовые основания подтверждай источниками, творческие формулировки не выдавай за нормы."
        ),
        accent="#81dfb9",
        glyph="D",
        training_lane="documents",
    ),
)

_BY_ID = {agent.id: agent for agent in _AGENTS}


def atlas_agents() -> tuple[AtlasAgent, ...]:
    return _AGENTS


def atlas_agent_catalog() -> list[dict[str, str]]:
    return [agent.public() for agent in _AGENTS]


def atlas_resolve_agent(value: str | None) -> AtlasAgent:
    selected = str(value or "atlas-tvr-a").strip().lower()
    agent = _BY_ID.get(selected)
    if agent is None:
        raise ValueError("atlas_agent_invalid")
    return agent


__all__ = [
    "AtlasAgent",
    "atlas_agent_catalog",
    "atlas_agents",
    "atlas_resolve_agent",
]
