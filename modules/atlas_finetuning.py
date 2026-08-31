"""Privacy-first preparation of reviewed Atlas fine-tuning datasets.

No function in this module uploads data or starts provider training. It builds
portable chat JSONL only from explicitly approved, redacted answer pairs and
keeps evaluation conversations out of the training split.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping


DATASET_VERSION = 1
DEFAULT_SYSTEM_PROMPT = (
    "Ты Atlas AI — точный и практичный помощник экосистемы T-Mod. "
    "Для юридических утверждений опирайся на доступные источники, не выдумывай "
    "статьи и ссылки, прямо отмечай неопределённость и соблюдай запрошенные "
    "пользователем формат и объём ответа."
)

_REDACTIONS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private_key",
        re.compile(
            r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?"
            r"-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "discord_token",
        re.compile(r"\b[A-Za-z\d_-]{20,}\.[A-Za-z\d_-]{5,}\.[A-Za-z\d_-]{20,}\b"),
    ),
    (
        "secret",
        re.compile(
            r"(?i)\b(?:x-api-key|api[_ -]?key|token|токен|password|пароль|"
            r"private[_ -]?key|секрет)\b\s*[:=]\s*[^\s,;]{6,}"
        ),
    ),
    ("email", re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")),
    ("discord_id", re.compile(r"(?<!\d)\d{15,20}(?!\d)")),
    (
        "pin",
        re.compile(r"(?i)\b(?:pin|пин|код доступа)\b\s*[:=]?\s*\d{6,12}\b"),
    ),
    (
        "ip_address",
        re.compile(
            r"(?<!\d)(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}"
            r"(?:25[0-5]|2[0-4]\d|1?\d?\d)(?!\d)"
        ),
    ),
)
_LOW_VALUE_PATTERNS = (
    re.compile(r"(?i)\b(?:нет|не найден[ао]?)\b.{0,35}\b(?:в библиотеке|в источниках|в базе)\b"),
    re.compile(r"(?i)^\s*(?:ошибка|error|internal server error|gateway timeout)\b"),
)


@dataclass(frozen=True, slots=True)
class PreparedCandidate:
    candidate_id: int
    thread_key: str
    agent_id: str
    user_text: str
    assistant_text: str
    model: str
    source_count: int
    flags: tuple[str, ...]
    checksum: str

    def review_record(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "status": "",
            "agent_id": self.agent_id,
            "model": self.model,
            "source_count": self.source_count,
            "flags": ",".join(self.flags),
            "prompt_preview": self.user_text[:360].replace("\n", " "),
            "answer_preview": self.assistant_text[:560].replace("\n", " "),
            "review_note": "",
        }

    def training_record(self) -> dict[str, Any]:
        return {
            "messages": [
                {"role": "system", "content": DEFAULT_SYSTEM_PROMPT},
                {"role": "user", "content": self.user_text},
                {"role": "assistant", "content": self.assistant_text},
            ]
        }


def redact_training_text(value: str) -> tuple[str, tuple[str, ...]]:
    text = str(value or "").replace("\x00", " ").strip()
    applied: list[str] = []
    for label, pattern in _REDACTIONS:
        replacement = f"[REDACTED_{label.upper()}]"
        text, count = pattern.subn(replacement, text)
        if count:
            applied.append(label)
    # Preserve useful public source URLs but discard fragments and query
    # strings, which frequently contain tickets or tracking identities.
    def clean_url(match: re.Match[str]) -> str:
        url = match.group(0)
        return url.split("?", 1)[0].split("#", 1)[0]

    text = re.sub(r"https?://[^\s<>]+", clean_url, text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text).strip()
    return text, tuple(sorted(set(applied)))


def _decoded_citations(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    try:
        decoded = json.loads(str(value or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return decoded if isinstance(decoded, list) else []


def prepare_candidates(rows: Iterable[Mapping[str, Any]]) -> tuple[list[PreparedCandidate], dict[str, int]]:
    accepted: list[PreparedCandidate] = []
    rejected = {
        "missing_pair": 0,
        "too_short": 0,
        "too_large": 0,
        "duplicate": 0,
    }
    seen: set[str] = set()
    for row in rows:
        user_text, user_redactions = redact_training_text(str(row.get("user_text") or ""))
        answer_text, answer_redactions = redact_training_text(str(row.get("assistant_text") or ""))
        if not user_text or not answer_text:
            rejected["missing_pair"] += 1
            continue
        if len(user_text) < 3 or len(answer_text) < 20:
            rejected["too_short"] += 1
            continue
        if len(user_text) > 20_000 or len(answer_text) > 40_000:
            rejected["too_large"] += 1
            continue
        checksum = hashlib.sha256(
            f"{user_text.casefold()}\0{answer_text.casefold()}".encode("utf-8")
        ).hexdigest()
        if checksum in seen:
            rejected["duplicate"] += 1
            continue
        seen.add(checksum)
        flags = set(user_redactions) | set(answer_redactions)
        if any(pattern.search(answer_text) for pattern in _LOW_VALUE_PATTERNS):
            flags.add("low_value_review")
        thread_key = hashlib.sha256(
            f"atlas-thread-v1:{int(row.get('thread_id') or 0)}".encode("utf-8")
        ).hexdigest()[:20]
        accepted.append(
            PreparedCandidate(
                candidate_id=int(row.get("feedback_id") or 0),
                thread_key=thread_key,
                agent_id=str(row.get("agent_id") or "atlas-tvr-a")[:80],
                user_text=user_text,
                assistant_text=answer_text,
                model=str(row.get("model") or "")[:160],
                source_count=len(_decoded_citations(row.get("citations", row.get("citations_json")))),
                flags=tuple(sorted(flags)),
                checksum=checksum,
            )
        )
    return accepted, rejected


def load_approvals(path: Path | None) -> set[int]:
    if path is None or not path.exists():
        return set()
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return {
            int(row["candidate_id"])
            for row in reader
            if str(row.get("status") or "").strip().lower() == "approved"
            and str(row.get("candidate_id") or "").strip().isdigit()
        }


def split_approved(
    candidates: Iterable[PreparedCandidate],
    approved_ids: set[int],
) -> tuple[list[PreparedCandidate], list[PreparedCandidate]]:
    approved = [item for item in candidates if item.candidate_id in approved_ids]
    groups = sorted({item.thread_key for item in approved})
    eval_groups = {
        key for key in groups
        if int(hashlib.sha256(f"atlas-eval-v1:{key}".encode()).hexdigest()[:8], 16) % 100 < 15
    }
    if len(groups) > 1 and not eval_groups:
        eval_groups.add(min(groups, key=lambda key: hashlib.sha256(key.encode()).hexdigest()))
    train = [item for item in approved if item.thread_key not in eval_groups]
    evaluation = [item for item in approved if item.thread_key in eval_groups]
    return train, evaluation


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> int:
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            count += 1
    return count


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_private(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        # Windows ACLs are inherited from the selected output directory.
        pass


def build_finetuning_bundle(
    rows: Iterable[Mapping[str, Any]],
    output_dir: Path,
    *,
    approvals_path: Path | None = None,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    candidates, rejected = prepare_candidates(rows)
    approved_ids = load_approvals(approvals_path)
    train, evaluation = split_approved(candidates, approved_ids)

    candidate_path = output_dir / "candidates.jsonl"
    review_path = output_dir / "review.csv"
    train_path = output_dir / "train.jsonl"
    eval_path = output_dir / "eval.jsonl"
    _write_jsonl(
        candidate_path,
        (
            {
                "candidate_id": item.candidate_id,
                "thread_key": item.thread_key,
                "agent_id": item.agent_id,
                "model": item.model,
                "source_count": item.source_count,
                "flags": list(item.flags),
                "checksum": item.checksum,
                **item.training_record(),
            }
            for item in candidates
        ),
    )
    with review_path.open("w", encoding="utf-8-sig", newline="") as handle:
        fields = list(PreparedCandidate(0, "", "", "", "", "", 0, (), "").review_record())
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(item.review_record() for item in candidates)
    _write_jsonl(train_path, (item.training_record() for item in train))
    _write_jsonl(eval_path, (item.training_record() for item in evaluation))
    for private_path in (candidate_path, review_path, train_path, eval_path):
        _make_private(private_path)

    manifest = {
        "dataset_version": DATASET_VERSION,
        "privacy": "redacted_offline_human_approval_required",
        "provider_upload_performed": False,
        "candidate_count": len(candidates),
        "approved_count": len(train) + len(evaluation),
        "train_count": len(train),
        "eval_count": len(evaluation),
        "rejected": rejected,
        "warnings": (
            ["No examples were approved; review review.csv before training."]
            if not approved_ids else []
        ) + (
            ["Evaluation split is empty; approve examples from at least two conversations."]
            if approved_ids and not evaluation else []
        ),
        "files": {
            path.name: {"sha256": _sha256(path), "bytes": path.stat().st_size}
            for path in (candidate_path, review_path, train_path, eval_path)
        },
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _make_private(manifest_path)
    return manifest


__all__ = [
    "DATASET_VERSION",
    "PreparedCandidate",
    "build_finetuning_bundle",
    "load_approvals",
    "prepare_candidates",
    "redact_training_text",
    "split_approved",
]
