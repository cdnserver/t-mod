"""OpenRouter adapter and strict domain validation for bill drafting."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any

import requests


BILL_EDITOR_MODEL = os.getenv(
    "TVRS_BILL_EDITOR_MODEL",
    "openai/gpt-4.1-mini",
).strip()


def _env_timeout() -> int:
    try:
        return max(
            10,
            int(os.getenv("TVRS_BILL_EDITOR_TIMEOUT_SECONDS", "45") or 45),
        )
    except (TypeError, ValueError):
        return 45


BILL_EDITOR_TIMEOUT_SECONDS = _env_timeout()
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
OPENROUTER_API_URL = os.getenv(
    "OPENROUTER_API_URL",
    "https://openrouter.ai/api/v1/chat/completions",
).strip()
OPENROUTER_REFERER = os.getenv("OPENROUTER_REFERER", "").strip()
OPENROUTER_TITLE = os.getenv("OPENROUTER_TITLE", "T-Mod").strip()

@dataclass(frozen=True, slots=True)
class BillEditorDraft:
    title: str
    summary: str
    materials: str | None
    decision_category: str
    implementation_plan: str
    leadership_actions: str
    clarification: str | None = None


def _clean(value: Any, *, maximum: int, minimum: int = 0) -> str:
    text = str(value or "").strip()
    if len(text) < minimum or len(text) > maximum:
        raise ValueError("bill_editor_ai_payload_invalid")
    return text


def extract_json_object(value: str) -> dict[str, Any]:
    raw = str(value or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("bill_editor_ai_payload_invalid")
        try:
            parsed = json.loads(raw[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ValueError("bill_editor_ai_payload_invalid") from exc
    if not isinstance(parsed, dict):
        raise ValueError("bill_editor_ai_payload_invalid")
    return parsed


def parse_bill_editor_draft(value: str | dict[str, Any]) -> BillEditorDraft:
    payload = extract_json_object(value) if isinstance(value, str) else dict(value)
    materials = _clean(payload.get("materials"), maximum=1000) or None
    clarification = _clean(payload.get("clarification"), maximum=600) or None
    return BillEditorDraft(
        title=_clean(payload.get("title"), minimum=5, maximum=180),
        summary=_clean(payload.get("summary"), minimum=20, maximum=3000),
        materials=materials,
        # The category is deliberately not delegated to AI. All bills submitted
        # through this editor use the ordinary procedure.
        decision_category="ordinary",
        implementation_plan=_clean(
            payload.get("implementation_plan"),
            minimum=5,
            maximum=1800,
        ),
        leadership_actions=_clean(
            payload.get("leadership_actions"),
            minimum=5,
            maximum=1800,
        ),
        clarification=clarification,
    )


def build_bill_editor_prompt(
    *,
    idea: str,
    desired_outcome: str,
    constraints_text: str,
    current_draft: dict[str, Any] | None = None,
) -> str:
    previous = ""
    if current_draft and current_draft.get("title"):
        previous = (
            "\nТекущий черновик, который нужно улучшить без выдумывания новых фактов:\n"
            + json.dumps(
                {
                    key: current_draft.get(key)
                    for key in (
                        "title",
                        "summary",
                        "materials",
                        "implementation_plan",
                        "leadership_actions",
                    )
                },
                ensure_ascii=False,
            )
        )
    return f"""
Ты — нейтральный юридико-операционный редактор Товарищества в Discord.
Преобразуй идею автора в ясный законопроект. Не добавляй фактов, имён,
сроков или полномочий, которых автор не сообщил. Не подменяй политическое
решение автора. Если критически важной информации не хватает, кратко укажи
это в clarification, но всё равно подготовь полезный черновик.

Законопроект будет рассмотрен по единой обычной процедуре. Не определяй
категорию, вес или порог решения и не объясняй механику голосования.
Не заявляй, что проект уже принят.

Требования к чистоте текста:
- текст законопроекта должен быть понятен человеку без технических знаний;
- не упоминай JSON, названия полей, модель ИИ, промпт, базу данных, внутренние
  идентификаторы, модули или устройство бота;
- не придумывай API, команды, таблицы, алгоритмы и другие детали реализации,
  если автор сам прямо их не указал;
- в summary помести только публикуемый текст предложения;
- организационные действия после принятия держи отдельно в
  implementation_plan и leadership_actions;
- не добавляй в текст заголовки вида «технические данные», «метаданные»,
  «категория решения» или «сгенерировано ИИ».

Идея автора:
{idea.strip()}

Желаемый результат:
{desired_outcome.strip()}

Ограничения и материалы автора:
{constraints_text.strip() or "Не указаны."}
{previous}

Верни только JSON-объект:
{{
  "title": "5–180 символов",
  "summary": "полный нормативный текст/суть до 3000 символов",
  "materials": "ссылки или исходные материалы до 1000 символов либо пусто",
  "implementation_plan": "что должно произойти после принятия",
  "leadership_actions": "конкретный чек-лист для руководства",
  "clarification": "что стоит уточнить автору либо пусто"
}}
""".strip()


def _response_text(body: dict[str, Any]) -> str:
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("bill_editor_ai_response_empty") from exc
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(item.get("text") or "")
            for item in content
            if isinstance(item, dict) and item.get("text")
        )
    raise ValueError("bill_editor_ai_response_empty")


def generate_bill_editor_draft(
    *,
    idea: str,
    desired_outcome: str,
    constraints_text: str,
    current_draft: dict[str, Any] | None = None,
) -> BillEditorDraft:
    if not OPENROUTER_API_KEY or OPENROUTER_API_KEY.startswith("YOUR_"):
        raise RuntimeError("bill_editor_ai_not_configured")
    prompt = build_bill_editor_prompt(
        idea=idea,
        desired_outcome=desired_outcome,
        constraints_text=constraints_text,
        current_draft=current_draft,
    )
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }
    if OPENROUTER_REFERER:
        headers["HTTP-Referer"] = OPENROUTER_REFERER
    if OPENROUTER_TITLE:
        headers["X-OpenRouter-Title"] = OPENROUTER_TITLE
    payload = {
        "model": BILL_EDITOR_MODEL,
        "temperature": 0.2,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Следуй фактам автора, пиши по-русски и возвращай только корректный JSON."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "response_format": {"type": "json_object"},
    }
    response = requests.post(
        OPENROUTER_API_URL,
        headers=headers,
        json=payload,
        timeout=BILL_EDITOR_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return parse_bill_editor_draft(_response_text(response.json()))


__all__ = [
    "BILL_EDITOR_MODEL",
    "BillEditorDraft",
    "build_bill_editor_prompt",
    "extract_json_object",
    "generate_bill_editor_draft",
    "parse_bill_editor_draft",
]
