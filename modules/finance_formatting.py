"""Pure money parsing and formatting for Finance and Craft."""

from __future__ import annotations

import re

from modules.finance_config import MAX_MONEY_AMOUNT


def parse_money(value: str, *, allow_zero: bool) -> int:
    raw = str(value or "").strip()
    if not raw or not re.fullmatch(r"[0-9\s.,_'’]+", raw):
        raise ValueError("Введите сумму цифрами, например: 1 250 000")
    compact = re.sub(r"[\s_'’]", "", raw)
    if "." in compact or "," in compact:
        if not re.fullmatch(r"[0-9]{1,3}(?:[.,][0-9]{3})+", compact):
            raise ValueError("Точки и запятые можно использовать только как разделители тысяч")
    elif not compact.isdigit():
        raise ValueError("Сумма не распознана")
    digits = re.sub(r"[^0-9]", "", compact)
    if not digits:
        raise ValueError("Сумма не распознана")
    amount = int(digits)
    if amount < 0 or (amount == 0 and not allow_zero):
        raise ValueError("Сумма должна быть больше нуля")
    if amount > MAX_MONEY_AMOUNT:
        raise ValueError("Сумма слишком большая")
    return amount


def money_text(amount: int | None) -> str:
    if amount is None:
        return "нет исходного отчёта"
    sign = "−" if int(amount) < 0 else ""
    return f"{sign}{abs(int(amount)):,}".replace(",", " ") + " $"
