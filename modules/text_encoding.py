"""Small guards for text crossing Windows environment/process boundaries."""

from __future__ import annotations


_MOJIBAKE_MARKERS = ("Ã", "Ð", "Ñ", "â€", "Â")


def repair_utf8_mojibake(value: object) -> str:
    """Recover UTF-8 text accidentally decoded as a Windows single-byte page."""

    original = str(value or "")

    def score(text: str) -> int:
        cyrillic = sum("\u0400" <= char <= "\u04ff" for char in text)
        noise = sum(text.count(marker) for marker in _MOJIBAKE_MARKERS)
        controls = sum(ord(char) < 32 and char not in "\n\r\t" for char in text)
        return cyrillic * 4 - noise * 8 - controls * 12

    best = original
    # Values crossing cmd.exe, PowerShell and Docker can be decoded more than
    # once.  Repair one layer at a time and stop as soon as quality no longer
    # improves; this keeps valid Russian text untouched.
    for _ in range(4):
        if not any(marker in best for marker in _MOJIBAKE_MARKERS):
            break
        current = best
        candidates: list[str] = []
        for encoding in ("cp1252", "latin1"):
            try:
                candidates.append(current.encode(encoding).decode("utf-8"))
            except (UnicodeEncodeError, UnicodeDecodeError):
                continue
        try:
            raw = bytearray()
            for char in current:
                try:
                    raw.extend(char.encode("latin1"))
                except UnicodeEncodeError:
                    raw.extend(char.encode("cp1252"))
            candidates.append(bytes(raw).decode("utf-8"))
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
        improved = max(candidates, key=score, default=current)
        if score(improved) <= score(current):
            break
        best = improved
    return best


__all__ = ["repair_utf8_mojibake"]
