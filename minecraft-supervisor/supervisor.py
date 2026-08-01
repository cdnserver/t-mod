"""Narrow Docker lifecycle bridge for the bundled Minecraft container."""

from __future__ import annotations

import http.client
import json
import os
import secrets
import socket
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


class SupervisorError(RuntimeError):
    pass


def read_token() -> str:
    try:
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SupervisorError("Supervisor token is unavailable") from exc
    if not 32 <= len(token) <= 200:
        raise SupervisorError("Supervisor token has an invalid length")
    return token


def docker_request(method: str, path: str) -> tuple[int, object]:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(45)
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
    return {
        "container": CONTAINER_NAME,
        "state": str(state.get("Status") or "unknown"),
        "running": bool(state.get("Running")),
        "started_at": str(state.get("StartedAt") or ""),
        "finished_at": str(state.get("FinishedAt") or ""),
    }


def lifecycle(action: str) -> dict[str, object]:
    if action not in ALLOWED_ACTIONS:
        raise SupervisorError("Action is not allowed")
    suffix = {
        "start": "/start",
        "stop": "/stop?t=30",
        "restart": "/restart?t=30",
    }[action]
    status, payload = docker_request("POST", container_path(suffix))
    if status not in {204, 304}:
        detail = payload.get("message") if isinstance(payload, dict) else payload
        raise SupervisorError(
            f"Docker returned status {status}: {detail or 'unknown error'}"
        )
    result = container_status()
    result.update({"ok": True, "action": action})
    return result


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
            self.respond(200, lifecycle(action))
        except SupervisorError as exc:
            self.respond(503, {"error": "supervisor_unavailable", "message": str(exc)})


if __name__ == "__main__":
    if not CONTAINER_NAME or "/" in CONTAINER_NAME:
        raise SystemExit("Invalid MINECRAFT_CONTAINER_NAME")
    read_token()
    print(f"Minecraft supervisor listening on :{PORT} for container {CONTAINER_NAME}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
