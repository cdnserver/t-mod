"""Temporary local, no-credentials browser demo for the SGL Case OS.

It serves only fixture data on localhost; it never contacts Discord, Atlas or a
forum. Run ``python tools/sgl_demo_server.py`` and open http://127.0.0.1:8137/sgl.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parents[1] / "web" / "sgl"
FONT_ROOT = Path(__file__).resolve().parents[1] / "web" / "consensus"
NOW = "2026-08-20T10:24:00+00:00"

CASES = [
    {
        "id": 1, "guild_id": 77, "case_number": 118, "channel_id": 501,
        "client_id": 101, "client_display": "Демо · Marta Hills", "client_nick": "Marta Hills",
        "lead_lawyer_id": 42, "lead_lawyer_display": "Saul Goodman", "secretary_id": 9,
        "secretary_display": "Kim Wexler", "status": "awaiting_link", "request_type": "Гражданский иск",
        "static_id": "M-114", "bank_account": "20485", "phone": "+1 555 010 111",
        "passport_url": "https://docs.example/client",
        "situation_text": "Демо-материалы: клиент передал две записи. Нужно сверить даты и подготовить иск.",
        "claim_link": None, "updated_at": NOW, "created_at": "2026-08-18T11:00:00+00:00",
        "event_count": 14, "receipt_count": 1, "archive_status": None,
        "discord_url": "https://discord.com/channels/77/501",
    },
    {
        "id": 2, "guild_id": 77, "case_number": 117, "channel_id": 502,
        "client_id": 102, "client_display": "Демо · J. Morales", "client_nick": "J. Morales",
        "lead_lawyer_id": 42, "lead_lawyer_display": "Saul Goodman", "secretary_id": None,
        "secretary_display": None, "status": "created", "request_type": "Апелляция", "static_id": "J-081",
        "bank_account": "20485", "phone": "+1 555 010 222", "passport_url": None,
        "situation_text": None, "claim_link": None, "updated_at": "2026-08-20T09:58:00+00:00",
        "created_at": "2026-08-20T09:31:00+00:00", "event_count": 3, "receipt_count": 0,
        "archive_status": None, "discord_url": "https://discord.com/channels/77/502",
    },
    {
        "id": 3, "guild_id": 77, "case_number": 116, "channel_id": 503,
        "client_id": 103, "client_display": "Демо · Amina Cole", "client_nick": "Amina Cole",
        "lead_lawyer_id": 61, "lead_lawyer_display": "Erin Brill", "secretary_id": 9,
        "secretary_display": "Kim Wexler", "status": "awaiting_close", "request_type": "Окружной иск",
        "static_id": "A-198", "bank_account": "20485", "phone": "+1 555 010 333", "passport_url": None,
        "situation_text": "Все обстоятельства подтверждены.", "claim_link": "https://forum.majestic-rp.ru/threads/116/",
        "updated_at": "2026-08-19T16:17:00+00:00", "created_at": "2026-08-16T13:00:00+00:00",
        "event_count": 27, "receipt_count": 2, "archive_status": None,
        "discord_url": "https://discord.com/channels/77/503",
    },
]
TASK = {
    "id": 7, "case_id": 1, "guild_id": 77, "case_number": 118, "title": "Сверить даты в двух записях",
    "description": "Сопоставить журнал Discord и вложение клиента.", "priority": "high", "status": "open",
    "owner_id": 42, "owner_display": "Saul Goodman", "due_at": "2026-08-20T15:00:00+00:00",
    "source": "atlas", "created_at": "2026-08-20T09:45:00+00:00",
}
TASKS = [
    TASK,
    {
        "id": 8, "case_id": 2, "guild_id": 77, "case_number": 117, "title": "Уточнить факты у клиента",
        "description": "Получить дату решения и ссылку на материалы апелляции.", "priority": "critical", "status": "open",
        "owner_id": 9, "owner_display": "Kim Wexler", "due_at": "2026-08-20T12:00:00+00:00",
        "source": "manual", "created_at": "2026-08-20T09:35:00+00:00",
    },
    {
        "id": 9, "case_id": 3, "guild_id": 77, "case_number": 116, "title": "Подтвердить получение ответа",
        "description": "Зафиксировать реакцию на новый комментарий Forum Watch.", "priority": "normal", "status": "done",
        "owner_id": 61, "owner_display": "Erin Brill", "due_at": "2026-08-19T18:00:00+00:00",
        "source": "forum_watch", "created_at": "2026-08-19T16:20:00+00:00", "completed_at": "2026-08-19T17:15:00+00:00",
    },
    {
        "id": 10, "case_id": 1, "guild_id": 77, "case_number": 118, "title": "Подготовить проект позиции",
        "description": "Собрать вывод Atlas в понятный план для клиента.", "priority": "low", "status": "open",
        "owner_id": None, "owner_display": None, "due_at": None,
        "source": "playbook:claim", "created_at": "2026-08-20T10:05:00+00:00",
    },
]
NOTE = {
    "id": 11, "kind": "risks", "question": "Проверь риски",
    "answer": "Есть конфликт двух дат в показаниях. До публикации иска нужно сверить исходную запись и запросить подтверждение времени.",
    "citations": [{"title": "Сообщение клиента"}], "agent_id": "atlas-claims",
    "response_mode": "balanced", "created_at": "2026-08-20T09:42:00+00:00",
}
DECISIONS = [
    {
        "id": 41, "case_id": 1, "guild_id": 77, "case_number": 118, "kind": "handoff",
        "title": "Подтвердить срок по исходной записи",
        "body": "Kim: сверить время записи с журналом Discord и до 18:00 вернуть подтверждённый дедлайн.",
        "status": "open", "target_user_id": 9, "target_display": "Kim Wexler",
        "created_by_id": 42, "created_by_display": "Saul Goodman", "created_at": "2026-08-20T09:48:00+00:00",
        "updated_at": "2026-08-20T09:48:00+00:00", "read_at": None, "acknowledged_at": None,
    },
    {
        "id": 40, "case_id": 1, "guild_id": 77, "case_number": 118, "kind": "risk",
        "title": "Две версии времени события",
        "body": "До публикации не использовать время из пересказа клиента как подтверждённый факт.",
        "status": "read", "target_user_id": None, "target_display": None,
        "created_by_id": 42, "created_by_display": "Saul Goodman", "created_at": "2026-08-20T09:43:00+00:00",
        "updated_at": "2026-08-20T09:46:00+00:00", "read_at": "2026-08-20T09:46:00+00:00", "acknowledged_at": None,
    },
]
PUBLICATION = {
    "id": 31, "case_id": 3, "guild_id": 77, "case_number": 116, "target_url": "https://forum.majestic-rp.ru/forums/court.42/",
    "title": "Иск SGL №116", "body": "[b]Обстоятельства[/b]\nВсе данные проверены.", "status": "published",
    "forum_url": "https://forum.majestic-rp.ru/threads/116/", "updated_at": "2026-08-19T16:17:00+00:00",
    "created_at": "2026-08-19T15:00:00+00:00",
}
OBSERVATION = {
    "id": 51, "publication_id": 31, "case_id": 3, "guild_id": 77, "case_number": 116,
    "forum_url": "https://forum.majestic-rp.ru/threads/116/", "thread_title": "Иск SGL №116",
    "thread_excerpt": "Добавлен новый комментарий ответчика с датой события.", "status": "changed",
    "last_checked_at": "2026-08-20T10:00:00+00:00", "last_changed_at": "2026-08-20T09:57:00+00:00",
    "last_notified_at": "2026-08-20T09:57:10+00:00", "last_error": None, "updated_at": "2026-08-20T10:00:00+00:00",
}
NOTIFICATION = {
    "id": 91, "guild_id": 77, "case_id": 3, "case_number": 116, "kind": "forum_changed",
    "severity": "warning", "title": "Изменился опубликованный иск",
    "body": "Добавлен новый комментарий ответчика с датой события.", "tab": "forum",
    "source": "forum_watch", "external_status": "sent", "created_at": "2026-08-20T09:57:00+00:00",
}
EVENTS = [
    {"id": 1, "case_id": 3, "guild_id": 77, "case_number": 116, "actor_id": None, "actor_display": "SGL Forum Watch", "action": "forum_topic_changed", "details": "", "created_at": "2026-08-20T09:57:00+00:00", "client_display": "Демо · Amina Cole"},
    {"id": 2, "case_id": 1, "guild_id": 77, "case_number": 118, "actor_id": 42, "actor_display": "Saul Goodman", "action": "task_created", "details": "", "created_at": "2026-08-20T09:45:00+00:00", "client_display": "Демо · Marta Hills"},
    {"id": 3, "case_id": 1, "guild_id": 77, "case_number": 118, "actor_id": None, "actor_display": "Atlas", "action": "atlas_analysis_saved", "details": "", "created_at": "2026-08-20T09:42:00+00:00", "client_display": "Демо · Marta Hills"},
]
MESSAGES = [
    {"id": 1, "discord_message_id": 118001, "origin": "discord", "author_display": "Marta Hills", "content": "Передала две записи и краткую хронологию: https://docs.example/timeline-m118", "created_at": "2026-08-20T09:11:00+00:00", "attachments": [{"name": "chronology-m118.pdf", "url": "https://files.example/chronology-m118.pdf", "content_type": "application/pdf"}, {"name": "entrance-camera-20-08.png", "url": "https://files.example/entrance-camera-20-08.png", "content_type": "image/png"}]},
    {"id": 2, "discord_message_id": 118002, "reply_to_discord_message_id": 118001, "origin": "web", "author_display": "Saul Goodman", "content": "Принял. Сверяем даты и вернёмся с планом действий.", "created_at": "2026-08-20T09:19:00+00:00", "attachments": [{"name": "brief-dates.txt", "url": "https://files.example/brief-dates.txt", "content_type": "text/plain"}]},
]
MEMBERS = [
    {"id": "42", "display_name": "Saul Goodman", "name": "saul"},
    {"id": "9", "display_name": "Kim Wexler", "name": "kim"},
    {"id": "61", "display_name": "Erin Brill", "name": "erin"},
]


def detail(number: int) -> dict:
    case = next((item for item in CASES if item["case_number"] == number), CASES[0])
    contract = {
        "contractNumber": f"SGL-{number:03d}", "contractDate": "20.08.2026",
        "lawyerName": case["lead_lawyer_display"], "lawyerStatic": "SG-001", "lawyerContact": "+1 555 010 001",
        "clientName": case["client_nick"], "clientPassport": case["static_id"], "clientContact": case["phone"],
        "servicePrice": "65000", "contractEndDate": "19.09.2026",
    }
    return {
        "case": case, "events": EVENTS if number in {116, 118} else [],
        "receipts": [{"id": 61, "case_id": 1, "case_number": 118, "total_amount": 100000, "lawyer_amount": 65000, "duty_amount": 35000, "court_label": "Окружная юрисдикция", "status": "proofs_submitted", "proof_services_url": "https://proof.example/services", "proof_duty_url": "https://proof.example/duty"}] if number == 118 else [],
        "messages": MESSAGES if number == 118 else [], "forum_publications": [PUBLICATION] if number == 116 else [],
        "forum_observations": [OBSERVATION] if number == 116 else [],
        "forum_snapshots": [{"id": 2, "snapshot_kind": "changed", "thread_excerpt": OBSERVATION["thread_excerpt"], "captured_at": NOW}, {"id": 1, "snapshot_kind": "initial", "thread_excerpt": "Исходная версия темы без комментария ответчика.", "captured_at": "2026-08-19T16:17:00+00:00"}] if number == 116 else [],
        "ai_notes": [NOTE] if number == 118 else [], "tasks": [task for task in TASKS if task["case_number"] == number],
        "notifications": [NOTIFICATION] if number == 116 else [], "contract": {"defaults": contract},
        "permissions": {"view": True, "write": True, "manage": True}, "forum": {"enabled": True},
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_: object) -> None:
        return

    def json(self, data: dict, status: int = 200) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def file(self, source: Path, content_type: str) -> None:
        payload = source.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def task(self, task: dict) -> dict:
        case = next((item for item in CASES if item["case_number"] == task["case_number"]), CASES[0])
        return {**task, "case_title": case["client_display"], "case_status": case["status"], "case_request_type": case["request_type"]}

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in {"/sgl", "/sgl/"} or path.startswith("/sgl/cases/"):
            return self.file(ROOT / "index.html", "text/html; charset=utf-8")
        if path.startswith("/sgl/assets/"):
            asset = ROOT / Path(path).name
            if asset.exists():
                types = {".css": "text/css", ".js": "application/javascript", ".svg": "image/svg+xml"}
                return self.file(asset, types.get(asset.suffix, "application/octet-stream"))
        if path == "/assets/fonts.css":
            return self.file(FONT_ROOT / "fonts.css", "text/css; charset=utf-8")
        if path.startswith("/assets/"):
            font = Path(path).name
            if font in {"manrope-cyrillic.woff2", "manrope-latin.woff2"}:
                return self.file(FONT_ROOT / font, "font/woff2")
        if path == "/api/sgl/bootstrap":
            return self.json({"mode": "management", "viewer": {"id": 42, "name": "Локальная демонстрация", "manager": True, "administrator": True, "csrf_token": "demo"}, "cases": {"items": CASES, "total": len(CASES), "statuses": ["created", "awaiting_link", "awaiting_close"]}, "archives": {"items": [], "total": 0}, "counts": {"open": 3}})
        if path == "/api/sgl/operations":
            queue = [
                {"id": "task:7", "kind": "task", "priority": "high", "case_number": 118, "case_id": 1, "title": TASK["title"], "detail": "Демо · Marta Hills", "tab": "overview", "due_at": TASK["due_at"], "task_id": 7},
                {"id": "forum:51", "kind": "forum", "priority": "high", "case_number": 116, "case_id": 3, "title": "Разобрать изменение опубликованного иска", "detail": OBSERVATION["thread_excerpt"], "tab": "forum", "observation_id": 51},
                {"id": "state:2:situation", "kind": "case_health", "priority": "high", "case_number": 117, "case_id": 2, "title": "Запросить обстоятельства дела", "detail": "Демо · J. Morales", "tab": "evidence"},
            ]
            return self.json({"queue": queue, "notifications": [NOTIFICATION], "events": EVENTS, "counts": {"open_cases": 3, "open_tasks": len([task for task in TASKS if task["status"] == "open"]), "pending_receipts": 1, "forum_alerts": 1, "atlas_notes": 1}, "health": {"discord": "ready", "atlas": "on_demand", "forum_watch": "scheduled", "forum_publish": "manual_ready", "forum_interval_seconds": 900}})
        if path == "/api/sgl/tasks":
            return self.json({"tasks": [self.task(task) for task in TASKS], "count": len(TASKS), "filters": {"status": None, "owner_id": None}})
        if path == "/api/sgl/forum/operations":
            return self.json({"publications": [PUBLICATION], "observations": [OBSERVATION], "alerts": [NOTIFICATION], "watch": {"enabled": True, "interval_seconds": 900, "scheduled": True, "tracked_publications": 1, "unacknowledged_alerts": 1, "last_checked_at": "2026-08-20T10:00:00+00:00", "runner": {"state": "idle", "last_finished_at": "2026-08-20T10:00:00+00:00"}}})
        if path == "/api/sgl/directory":
            return self.json({"clients": [{"id": 1, "discord_user_id": 101, "client_nick": "Marta Hills", "static_id": "M-114", "phone": "+1 555 010 111"}], "lawyers": [{"id": 5, "discord_user_id": 42, "lawyer_nick": "Saul Goodman", "static_id": "SG-001", "phone": "+1 555 010 001", "email": "saul@sgl.example"}]})
        if path == "/api/sgl/members":
            needle = str(parse_qs(urlparse(self.path).query).get("q", [""])[0]).casefold()
            return self.json({"members": [item for item in MEMBERS if not needle or needle in (item["id"] + " " + item["display_name"] + " " + item["name"]).casefold()]})
        if path.startswith("/api/sgl/cases/"):
            number = int(path.split("/")[4])
            if path.endswith("/decisions"):
                decisions = [item for item in DECISIONS if item["case_number"] == number]
                return self.json({"case_number": number, "decisions": decisions, "count": len(decisions), "include_resolved": True})
            if path.endswith("/messages"):
                return self.json({"case_number": number, "messages": []})
            if path.endswith("/contract"):
                return self.json({"case_number": number, "defaults": detail(number)["contract"]["defaults"]})
            return self.json(detail(number))
        self.send_response(404); self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        self.write_action()

    def do_PATCH(self) -> None:  # noqa: N802
        self.write_action()

    def write_action(self) -> None:
        size = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(size) if size else b""
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {}
        path = urlparse(self.path).path
        if path.endswith("/atlas"):
            note = {"id": 99, "kind": "analysis", "answer": "Atlas demo: контекст проверен, следующий шаг сформирован.", "citations": [], "agent_id": "atlas-claims", "created_at": NOW}
            return self.json({"note": note, "analysis": {"answer": note["answer"]}})
        if path.endswith("/messages"):
            return self.json({"message": {"id": 3}}, 201)
        if "/decisions/" in path:
            decision_id = int(path.rsplit("/", 1)[-1])
            decision = next((item for item in DECISIONS if item["id"] == decision_id), DECISIONS[0])
            decision["status"] = payload.get("status") or decision["status"]
            decision["updated_at"] = NOW
            if decision["status"] in {"read", "acknowledged"}:
                decision["read_at"] = decision.get("read_at") or NOW
            if decision["status"] == "acknowledged":
                decision["acknowledged_at"] = NOW
            return self.json({"decision": decision})
        if path.endswith("/decisions"):
            case_number = int(path.split("/")[4])
            target_id = int(payload["target_user_id"]) if payload.get("target_user_id") else None
            decision = {
                "id": max(item["id"] for item in DECISIONS) + 1, "case_id": next(item["id"] for item in CASES if item["case_number"] == case_number),
                "guild_id": 77, "case_number": case_number, "kind": payload.get("kind") or "decision",
                "title": payload.get("title") or "Новая запись", "body": payload.get("body") or "Контекст не указан.",
                "status": "open", "target_user_id": target_id,
                "target_display": {42: "Saul Goodman", 9: "Kim Wexler", 61: "Erin Brill"}.get(target_id) if target_id else None,
                "created_by_id": 42, "created_by_display": "Saul Goodman", "created_at": NOW, "updated_at": NOW,
                "read_at": None, "acknowledged_at": None,
            }
            DECISIONS.insert(0, decision)
            return self.json({"decision": decision}, 201)
        if "/tasks/" in path:
            task_id = int(path.rsplit("/", 1)[-1])
            task = next((item for item in TASKS if item["id"] == task_id), TASK)
            for key in {"title", "description", "priority", "owner_id", "due_at", "status"}:
                if key in payload:
                    task[key] = payload[key] or None
            if task.get("owner_id"):
                task["owner_display"] = {42: "Saul Goodman", 9: "Kim Wexler", 61: "Erin Brill"}.get(int(task["owner_id"]), "Участник " + str(task["owner_id"]))
            elif "owner_id" in payload:
                task["owner_display"] = None
            if task.get("status") == "done":
                task["completed_at"] = NOW
            return self.json({"task": self.task(task)})
        if path.endswith("/tasks"):
            case_number = int(path.split("/")[4])
            case = next((item for item in CASES if item["case_number"] == case_number), CASES[0])
            task = {"id": max(item["id"] for item in TASKS) + 1, "case_id": case["id"], "guild_id": 77, "case_number": case_number, "title": payload.get("title") or "Новая задача", "description": payload.get("description") or None, "priority": payload.get("priority") or "normal", "status": "open", "owner_id": int(payload["owner_id"]) if payload.get("owner_id") else None, "owner_display": None, "due_at": payload.get("due_at") or None, "source": "manual", "created_at": NOW}
            if task["owner_id"]:
                task["owner_display"] = {42: "Saul Goodman", 9: "Kim Wexler", 61: "Erin Brill"}.get(task["owner_id"], "Участник " + str(task["owner_id"]))
            TASKS.append(task)
            return self.json({"task": self.task(task)}, 201)
        if path.endswith("/acknowledge"):
            return self.json({"notification": NOTIFICATION})
        if path.endswith("/forum/watch"):
            return self.json({"summary": {"checked": 1, "changed": 0, "alerts": 0}})
        if path.endswith("/contract"):
            return self.json({"values": detail(int(path.split("/")[4]))["contract"]["defaults"], "pages": 3, "message_ids": [1]}, 201)
        if path.endswith("/close"):
            return self.json({"case": {**detail(int(path.split("/")[4]))["case"], "status": "closed"}})
        return self.json({"case": CASES[0]}, 201)


if __name__ == "__main__":
    print("SGL demo: http://127.0.0.1:8137/sgl", flush=True)
    ThreadingHTTPServer(("127.0.0.1", 8137), Handler).serve_forever()
