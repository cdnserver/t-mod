"""Small guards for text crossing Windows environment/process boundaries."""

from __future__ import annotations


_MOJIBAKE_MARKERS = ("Ð", "Ñ", "â€", "Â")


def repair_utf8_mojibake(value: object) -> str:
    """Recover UTF-8 text accidentally decoded as a Windows single-byte page."""

    original = str(value or "")
    if not any(marker in original for marker in _MOJIBAKE_MARKERS):
        return original

    def score(text: str) -> int:
        cyrillic = sum("\u0400" <= char <= "\u04ff" for char in text)
        noise = sum(text.count(marker) for marker in _MOJIBAKE_MARKERS)
        controls = sum(ord(char) < 32 and char not in "\n\r\t" for char in text)
        return cyrillic * 4 - noise * 8 - controls * 12

    best = original
    for encoding in ("cp1252", "latin1"):
        try:
            candidate = original.encode(encoding).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if score(candidate) > score(best):
            best = candidate
    try:
        raw = bytearray()
        for char in original:
            try:
                raw.extend(char.encode("latin1"))
            except UnicodeEncodeError:
                raw.extend(char.encode("cp1252"))
        candidate = bytes(raw).decode("utf-8")
        if score(candidate) > score(best):
            best = candidate
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    return best


__all__ = ["repair_utf8_mojibake"]
