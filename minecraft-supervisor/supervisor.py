"""Narrow Docker lifecycle bridge for the bundled Minecraft container."""

from __future__ import annotations

import http.client
import json
import os
import secrets
import socket
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote


SOCKET_PATH = os.getenv("DOCKER_SOCKET", "/var/run/docker.sock")
CONTAINER_NAME = os.getenv("MINECRAFT_CONTAINER_NAME", "minecraft").strip()
TOKEN_FILE = Path(
    os.getenv(
        "MINECRAFT_SUPERVISOR_TOKEN_FILE", "/run/secrets/minecraft_supervisor_token"
    )
)
PORT = int(os.getenv("MINECRAFT_SUPERVISOR_PORT", "8791"))
ALLOWED_ACTIONS = frozenset({"start", "stop", "restart"})
try:
    STOP_GRACE_SECONDS = max(
        1,
        min(30, int(os.getenv("MINECRAFT_STOP_GRACE_SECONDS", "10"))),
    )
except (TypeError, ValueError):
    STOP_GRACE_SECONDS = 10

_operation_lock = threading.Lock()
_operation: dict[str, object] | None = None


class SupervisorError(RuntimeError):
    pass


class SupervisorBusyError(SupervisorError):
    pass


def read_token() -> str:
    try:
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SupervisorError("Supervisor token is unavailable") from exc
    if not 32 <= len(token) <= 200:
        raise SupervisorError("Supervisor token has an invalid length")
    return token


def docker_request(
    method: str,
    path: str,
    *,
    timeout_seconds: float = 3.0,
) -> tuple[int, object]:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(max(0.5, float(timeout_seconds)))
    try:
        client.connect(SOCKET_PATH)
        request = (
            f"{method} {path} HTTP/1.1\r\n"
            "Host: docker\r\n"
            "Connection: close\r\n"
            "Content-Length: 0\r\n\r\n"
        )
        client.sendall(request.encode("ascii"))
        response = http.client.HTTPResponse(client)
        response.begin()
        raw = response.read()
        payload: object = None
        if raw:
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                payload = raw.decode("utf-8", errors="replace")[:1000]
        return response.status, payload
    except OSError as exc:
        raise SupervisorError("Docker Engine is unavailable") from exc
    finally:
        client.close()


def container_path(suffix: str) -> str:
    return f"/containers/{quote(CONTAINER_NAME, safe='')}{suffix}"


def container_status() -> dict[str, object]:
    status, payload = docker_request("GET", container_path("/json"))
    if status != 200 or not isinstance(payload, dict):
        raise SupervisorError(f"Docker returned status {status}")
    state = payload.get("State") if isinstance(payload.get("State"), dict) else {}
    result: dict[str, object] = {
        "container": CONTAINER_NAME,
        "state": str(state.get("Status") or "unknown"),
        "running": bool(state.get("Running")),
        "started_at": str(state.get("StartedAt") or ""),
        "finished_at": str(state.get("FinishedAt") or ""),
    }
    operation = operation_status()
    if operation is not None:
        result["operation"] = operation
    return result


def lifecycle(action: str) -> dict[str, object]:
    if action not in ALLOWED_ACTIONS:
        raise SupervisorError("Action is not allowed")
    suffix = {
        "start": "/start",
        "stop": f"/stop?t={STOP_GRACE_SECONDS}",
        "restart": f"/restart?t={STOP_GRACE_SECONDS}",
    }[action]
    status, payload = docker_request(
        "POST",
        container_path(suffix),
        timeout_seconds=STOP_GRACE_SECONDS + 8,
    )
    if status not in {204, 304}:
        detail = payload.get("message") if isinstance(payload, dict) else payload
        raise SupervisorError(
            f"Docker returned status {status}: {detail or 'unknown error'}"
        )
    result = container_status()
    result.update({"ok": True, "action": action})
    return result


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def operation_status() -> dict[str, object] | None:
    with _operation_lock:
        return dict(_operation) if _operation is not None else None


def _run_lifecycle(operation_id: str, action: str) -> None:
    global _operation
    with _operation_lock:
        if _operation is None or _operation.get("id") != operation_id:
            return
        _operation.update({"status": "running", "started_at": _utc_now()})
    try:
        result = lifecycle(action)
    except Exception as exc:  # keep the supervisor alive after Docker failures
        with _operation_lock:
            if _operation is not None and _operation.get("id") == operation_id:
                _operation.update(
                    {
                        "status": "failed",
                        "finished_at": _utc_now(),
                        "error": str(exc)[:500],
                    }
                )
        return
    with _operation_lock:
        if _operation is not None and _operation.get("id") == operation_id:
            _operation.update(
                {
                    "status": "completed",
                    "finished_at": _utc_now(),
                    "result_state": str(result.get("state") or "unknown"),
                }
            )


def schedule_lifecycle(action: str) -> dict[str, object]:
    """Accept a lifecycle operation without blocking the Reactor HTTP request."""

    global _operation
    if action not in ALLOWED_ACTIONS:
        raise SupervisorError("Action is not allowed")
    with _operation_lock:
        active = _operation is not None and _operation.get("status") in {
            "queued",
            "running",
        }
        if active:
            if _operation is not None and _operation.get("action") == action:
                return {
                    "ok": True,
                    "accepted": True,
                    "duplicate": True,
                    "action": action,
                    "state": str(_operation.get("state") or "changing"),
                    "operation": dict(_operation),
                }
            raise SupervisorBusyError("Another Minecraft lifecycle operation is active")
        public_state = {
            "start": "starting",
            "stop": "stopping",
            "restart": "restarting",
        }[action]
        operation_id = secrets.token_hex(8)
        _operation = {
            "id": operation_id,
            "action": action,
            "status": "queued",
            "state": public_state,
            "accepted_at": _utc_now(),
        }
        snapshot = dict(_operation)
    try:
        threading.Thread(
            target=_run_lifecycle,
            args=(operation_id, action),
            name=f"minecraft-{action}-{operation_id}",
            daemon=True,
        ).start()
    except RuntimeError as exc:
        with _operation_lock:
            if _operation is not None and _operation.get("id") == operation_id:
                _operation.update(
                    {
                        "status": "failed",
                        "finished_at": _utc_now(),
                        "error": str(exc)[:500],
                    }
                )
        raise SupervisorError("Lifecycle worker could not be started") from exc
    return {
        "ok": True,
        "accepted": True,
        "action": action,
        "state": public_state,
        "operation": snapshot,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "TModMinecraftSupervisor/1"

    def log_message(self, format: str, *args: object) -> None:
        print(f"[minecraft-supervisor] {self.address_string()} {format % args}")

    def respond(self, status: int, payload: dict[str, object]) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def authorized(self) -> bool:
        supplied = self.headers.get("Authorization", "")
        expected = f"Bearer {read_token()}"
        return secrets.compare_digest(supplied, expected)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self.respond(200, {"status": "ok"})
            return
        try:
            if self.path != "/v1/status":
                self.respond(404, {"error": "not_found"})
                return
            if not self.authorized():
                self.respond(401, {"error": "unauthorized"})
                return
            self.respond(200, container_status())
        except SupervisorError as exc:
            self.respond(503, {"error": "supervisor_unavailable", "message": str(exc)})

    def do_POST(self) -> None:  # noqa: N802
        try:
            if not self.authorized():
                self.respond(401, {"error": "unauthorized"})
                return
            action = self.path.removeprefix("/v1/")
            if self.path != f"/v1/{action}" or action not in ALLOWED_ACTIONS:
                self.respond(404, {"error": "not_found"})
                return
            self.respond(202, schedule_lifecycle(action))
        except SupervisorBusyError as exc:
            self.respond(
                409,
                {"error": "operation_in_progress", "message": str(exc)},
            )
        except SupervisorError as exc:
            self.respond(503, {"error": "supervisor_unavailable", "message": str(exc)})


if __name__ == "__main__":
    if not CONTAINER_NAME or "/" in CONTAINER_NAME:
        raise SystemExit("Invalid MINECRAFT_CONTAINER_NAME")
    read_token()
    print(f"Minecraft supervisor listening on :{PORT} for container {CONTAINER_NAME}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
