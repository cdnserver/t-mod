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


DATASET_VERSION = 2
DEFAULT_PROJECT_CODE = "majestic-rp"
DEFAULT_SYSTEM_PROMPT = (
    "Ты Atlas AI — точный и практичный помощник экосистемы T-Mod. "
    "Для юридических утверждений опирайся на доступные источники, не выдумывай "
    "статьи и ссылки, прямо отмечай неопределённость и соблюдай запрошенные "
    "пользователем формат и объём ответа."
)
_SCOPE_CODE_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")

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


class AtlasDatasetScopeError(ValueError):
    """Raised when a dataset would combine independent Atlas model lanes.

    A fine-tuning file is deliberately scoped to one project and one agent by
    default.  Mixing them makes it impossible to audit which rules, style and
    user feedback influenced a model release.
    """


def _scope_code(
    value: object,
    *,
    fallback: str | None = None,
    error: str,
) -> str:
    candidate = str(value or fallback or "").strip().lower()
    if not _SCOPE_CODE_RE.fullmatch(candidate):
        raise AtlasDatasetScopeError(error)
    return candidate


def _training_lane_for_agent(agent_id: str) -> str:
    """Resolve an agent's stable training lane without trusting row input."""

    try:
        from modules.atlas_agents import atlas_resolve_agent

        return str(atlas_resolve_agent(agent_id).training_lane)
    except (ImportError, ValueError, AttributeError) as exc:
        raise AtlasDatasetScopeError("atlas_finetuning_agent_unregistered") from exc


def atlas_agent_system_prompt(agent_id: str) -> str:
    """Return the same role boundary that the selected agent receives at run time."""

    try:
        from modules.atlas_agents import atlas_resolve_agent

        agent = atlas_resolve_agent(agent_id)
    except (ImportError, ValueError) as exc:
        raise AtlasDatasetScopeError("atlas_finetuning_agent_unregistered") from exc
    return f"{DEFAULT_SYSTEM_PROMPT}\n\nРоль агента: {agent.instruction}"


@dataclass(frozen=True, slots=True)
class PreparedCandidate:
    candidate_id: int
    thread_key: str
    agent_id: str
    user_text: str
    assistant_text: str
    model: str
    model_provider: str
    model_release: str
    server_code: str
    faction_code: str
    source_count: int
    flags: tuple[str, ...]
    checksum: str
    project_code: str = DEFAULT_PROJECT_CODE
    training_lane: str = "general"

    def review_record(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "status": "",
            "project_code": self.project_code,
            "agent_id": self.agent_id,
            "training_lane": self.training_lane,
            "model": self.model,
            "model_provider": self.model_provider,
            "model_release": self.model_release,
            "server_code": self.server_code,
            "faction_code": self.faction_code,
            "source_count": self.source_count,
            "flags": ",".join(self.flags),
            "checksum": self.checksum,
            "prompt_preview": self.user_text[:360].replace("\n", " "),
            "answer_preview": self.assistant_text[:560].replace("\n", " "),
            "review_note": "",
        }

    def training_record(self) -> dict[str, Any]:
        return {
            "messages": [
                {"role": "system", "content": atlas_agent_system_prompt(self.agent_id)},
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


def prepare_candidates(
    rows: Iterable[Mapping[str, Any]],
    *,
    project_code: str | None = None,
    agent_id: str | None = None,
) -> tuple[list[PreparedCandidate], dict[str, int]]:
    """Redact candidate rows while preserving their project and agent boundary.

    ``project_code`` and ``agent_id`` are assertions supplied by the caller.
    Rows that disagree are excluded rather than relabelled.  Old direct callers
    without project metadata remain compatible only with the legacy default
    project, and the result is visibly marked for reviewer attention.
    """

    expected_project = (
        _scope_code(project_code, error="atlas_finetuning_project_invalid")
        if project_code is not None
        else None
    )
    expected_agent = (
        _scope_code(agent_id, error="atlas_finetuning_agent_invalid")
        if agent_id is not None
        else None
    )
    accepted: list[PreparedCandidate] = []
    rejected = {
        "missing_pair": 0,
        "too_short": 0,
        "too_large": 0,
        "duplicate": 0,
        "project_scope_mismatch": 0,
        "agent_scope_mismatch": 0,
        "invalid_scope": 0,
    }
    seen: set[str] = set()
    for row in rows:
        row_flags: set[str] = set()
        raw_project = str(row.get("project_code") or "").strip().lower()
        if not raw_project:
            # Historical candidate rows predate project federation.  They can
            # only remain compatible with the original Majestic project; a new
            # project must never inherit ambiguous feedback.
            if expected_project not in {None, DEFAULT_PROJECT_CODE}:
                rejected["project_scope_mismatch"] += 1
                continue
            clean_project = DEFAULT_PROJECT_CODE
            row_flags.add("legacy_project_scope")
        else:
            try:
                clean_project = _scope_code(raw_project, error="atlas_finetuning_project_invalid")
            except AtlasDatasetScopeError:
                rejected["invalid_scope"] += 1
                continue
        raw_agent = str(row.get("agent_id") or "atlas-tvr-a").strip().lower()
        try:
            clean_agent = _scope_code(raw_agent, error="atlas_finetuning_agent_invalid")
            training_lane = _training_lane_for_agent(clean_agent)
        except AtlasDatasetScopeError:
            rejected["invalid_scope"] += 1
            continue
        if expected_project is not None and clean_project != expected_project:
            rejected["project_scope_mismatch"] += 1
            continue
        if expected_agent is not None and clean_agent != expected_agent:
            rejected["agent_scope_mismatch"] += 1
            continue
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
        flags = row_flags | set(user_redactions) | set(answer_redactions)
        if any(pattern.search(answer_text) for pattern in _LOW_VALUE_PATTERNS):
            flags.add("low_value_review")
        thread_key = hashlib.sha256(
            f"atlas-thread-v2:{clean_project}:{int(row.get('thread_id') or 0)}".encode("utf-8")
        ).hexdigest()[:20]
        accepted.append(
            PreparedCandidate(
                candidate_id=int(row.get("feedback_id") or 0),
                thread_key=thread_key,
                agent_id=clean_agent,
                user_text=user_text,
                assistant_text=answer_text,
                model=str(row.get("model") or "")[:160],
                model_provider=str(row.get("model_provider") or "")[:80],
                model_release=str(row.get("model_release") or "")[:120],
                server_code=str(row.get("answer_server_code") or row.get("server_code") or "")[:80],
                faction_code=str(row.get("answer_faction_code") or row.get("faction_code") or "")[:80],
                source_count=len(_decoded_citations(row.get("citations", row.get("citations_json")))),
                flags=tuple(sorted(flags)),
                checksum=checksum,
                project_code=clean_project,
                training_lane=training_lane,
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


def load_approval_checksums(path: Path | None) -> dict[int, str]:
    """Load checksum-bound approvals emitted by the current review template.

    A reviewer approves the exact redacted pair they saw.  Reusing the same
    feedback id after an answer changes is intentionally not an approval.
    """

    if path is None or not path.exists():
        return {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return {
            int(row["candidate_id"]): str(row.get("checksum") or "").strip().lower()
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


def _assert_bundle_scope(
    candidates: Iterable[PreparedCandidate],
    *,
    project_code: str | None,
    agent_id: str | None,
    allow_mixed_projects: bool,
    allow_mixed_agents: bool,
) -> dict[str, Any]:
    items = list(candidates)
    projects = sorted({item.project_code for item in items})
    agents = sorted({item.agent_id for item in items})
    lanes = sorted({item.training_lane for item in items})
    expected_project = (
        _scope_code(project_code, error="atlas_finetuning_project_invalid")
        if project_code is not None
        else None
    )
    expected_agent = (
        _scope_code(agent_id, error="atlas_finetuning_agent_invalid")
        if agent_id is not None
        else None
    )
    if expected_project is not None and any(item != expected_project for item in projects):
        raise AtlasDatasetScopeError("atlas_finetuning_project_scope_mismatch")
    if expected_agent is not None and any(item != expected_agent for item in agents):
        raise AtlasDatasetScopeError("atlas_finetuning_agent_scope_mismatch")
    if len(projects) > 1 and not allow_mixed_projects:
        raise AtlasDatasetScopeError("atlas_finetuning_cross_project_export_forbidden")
    if len(agents) > 1 and not allow_mixed_agents:
        raise AtlasDatasetScopeError("atlas_finetuning_cross_agent_export_forbidden")
    return {
        "project_code": expected_project or (projects[0] if len(projects) == 1 else None),
        "agent_id": expected_agent or (agents[0] if len(agents) == 1 else None),
        "projects": projects,
        "agents": agents,
        "training_lanes": lanes,
        "mixed_projects_explicit": bool(len(projects) > 1 and allow_mixed_projects),
        "mixed_agents_explicit": bool(len(agents) > 1 and allow_mixed_agents),
    }


def _assert_output_scope(output_dir: Path, scope: Mapping[str, Any]) -> None:
    """Prevent a reused folder from silently replacing another training lane."""

    manifest_path = output_dir / "manifest.json"
    if not manifest_path.exists():
        return
    try:
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return
    old_scope = previous.get("scope") if isinstance(previous, dict) else None
    if not isinstance(old_scope, dict):
        return
    for key in ("project_code", "agent_id"):
        before = str(old_scope.get(key) or "")
        after = str(scope.get(key) or "")
        if before and after and before != after:
            raise AtlasDatasetScopeError("atlas_finetuning_output_scope_mismatch")


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
    project_code: str | None = None,
    agent_id: str | None = None,
    allow_mixed_projects: bool = False,
    allow_mixed_agents: bool = False,
) -> dict[str, Any]:
    # Validate caller-provided boundary before creating any filesystem output.
    if project_code is not None:
        _scope_code(project_code, error="atlas_finetuning_project_invalid")
    if agent_id is not None:
        clean_agent = _scope_code(agent_id, error="atlas_finetuning_agent_invalid")
        _training_lane_for_agent(clean_agent)
    output_dir.mkdir(parents=True, exist_ok=True)
    candidates, rejected = prepare_candidates(
        rows,
        project_code=project_code,
        agent_id=agent_id,
    )
    scope = _assert_bundle_scope(
        candidates,
        project_code=project_code,
        agent_id=agent_id,
        allow_mixed_projects=allow_mixed_projects,
        allow_mixed_agents=allow_mixed_agents,
    )
    _assert_output_scope(output_dir, scope)
    reviewed_ids = load_approvals(approvals_path)
    approval_checksums = load_approval_checksums(approvals_path)
    approved_ids = {
        item.candidate_id
        for item in candidates
        if item.candidate_id in reviewed_ids
        and approval_checksums.get(item.candidate_id) == item.checksum
    }
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
                "project_code": item.project_code,
                "agent_id": item.agent_id,
                "training_lane": item.training_lane,
                "model": item.model,
                "model_provider": item.model_provider,
                "model_release": item.model_release,
                "server_code": item.server_code,
                "faction_code": item.faction_code,
                "source_count": item.source_count,
                "flags": list(item.flags),
                "checksum": item.checksum,
                **item.training_record(),
            }
            for item in candidates
        ),
    )
    with review_path.open("w", encoding="utf-8-sig", newline="") as handle:
        fields = list(
            PreparedCandidate(0, "", "", "", "", "", "", "", "", "", 0, (), "").review_record()
        )
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
        "scope": scope,
        "candidate_count": len(candidates),
        "approved_count": len(train) + len(evaluation),
        "train_count": len(train),
        "eval_count": len(evaluation),
        "rejected": rejected,
        "warnings": (
            ["No examples were approved; review review.csv before training."]
            if not approved_ids else []
        ) + (
            ["Some approvals were ignored because their candidate checksum changed or is missing."]
            if reviewed_ids and len(approved_ids) < len(reviewed_ids) else []
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
    "DEFAULT_PROJECT_CODE",
    "AtlasDatasetScopeError",
    "PreparedCandidate",
    "atlas_agent_system_prompt",
    "build_finetuning_bundle",
    "load_approval_checksums",
    "load_approvals",
    "prepare_candidates",
    "redact_training_text",
    "split_approved",
]
