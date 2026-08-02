"""Small, allow-listed RCON client for the bundled Minecraft server."""

from __future__ import annotations

import os
import re
import socket
import struct
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class MinecraftControlError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MinecraftConfig:
    host: str
    port: int
    password_file: Path
    timeout_seconds: float

    @property
    def configured(self) -> bool:
        return bool(self.host and self.port > 0 and self.password_file.exists())


def minecraft_config() -> MinecraftConfig:
    raw_port = os.getenv("MINECRAFT_RCON_PORT", "25575").strip()
    raw_timeout = os.getenv("MINECRAFT_RCON_TIMEOUT_SECONDS", "3").strip()
    try:
        port = max(1, min(65535, int(raw_port)))
    except (TypeError, ValueError):
        port = 25575
    try:
        timeout = max(0.5, min(10.0, float(raw_timeout)))
    except (TypeError, ValueError):
        timeout = 3.0
    return MinecraftConfig(
        host=os.getenv("MINECRAFT_RCON_HOST", "minecraft").strip() or "minecraft",
        port=port,
        password_file=Path(
            os.getenv(
                "MINECRAFT_RCON_PASSWORD_FILE",
                "/run/secrets/minecraft_rcon_password",
            )
        ),
        timeout_seconds=timeout,
    )


def _read_password(config: MinecraftConfig) -> str:
    try:
        password = config.password_file.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise MinecraftControlError("Секрет RCON недоступен.") from exc
    if not 16 <= len(password) <= 200:
        raise MinecraftControlError("Секрет RCON имеет небезопасную длину.")
    return password


def _supervisor_token() -> str:
    path = Path(
        os.getenv(
            "MINECRAFT_SUPERVISOR_TOKEN_FILE",
            "/run/secrets/minecraft_supervisor_token",
        )
    )
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise MinecraftControlError("Контроллер жизненного цикла не настроен.") from exc
    if not 32 <= len(token) <= 200:
        raise MinecraftControlError("Секрет контроллера имеет небезопасную длину.")
    return token


def minecraft_supervisor(action: str) -> dict[str, Any]:
    selected = str(action or "").strip().lower()
    if selected not in {"status", "start", "stop", "restart"}:
        raise MinecraftControlError("Действие контроллера Minecraft не разрешено.")
    base_url = (
        os.getenv(
            "MINECRAFT_SUPERVISOR_URL",
            "http://minecraft-supervisor:8791",
        )
        .strip()
        .rstrip("/")
    )
    if not base_url.startswith("http://"):
        raise MinecraftControlError("Некорректный адрес контроллера Minecraft.")
    request = Request(
        f"{base_url}/v1/{selected}",
        method="GET" if selected == "status" else "POST",
        headers={"Authorization": f"Bearer {_supervisor_token()}"},
    )
    try:
        timeout = 2 if selected == "status" else 5
        with urlopen(request, timeout=timeout) as response:  # noqa: S310
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = ""
        try:
            error_payload = json.loads(exc.read().decode("utf-8"))
            if isinstance(error_payload, dict):
                detail = str(error_payload.get("message") or "").strip()
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            pass
        message = detail or f"HTTP {exc.code}"
        raise MinecraftControlError(
            f"Контроллер Minecraft отклонил запрос: {message[:300]}"
        ) from exc
    except (URLError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MinecraftControlError(
            "Контроллер жизненного цикла Minecraft недоступен."
        ) from exc
    if not isinstance(payload, dict):
        raise MinecraftControlError("Контроллер Minecraft вернул неверный ответ.")
    return payload


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = sock.recv(size - len(chunks))
        if not chunk:
            raise MinecraftControlError("Minecraft закрыл RCON-соединение.")
        chunks.extend(chunk)
    return bytes(chunks)


def _packet(request_id: int, packet_type: int, payload: str) -> bytes:
    encoded = str(payload).encode("utf-8")
    body = struct.pack("<ii", int(request_id), int(packet_type)) + encoded + b"\x00\x00"
    return struct.pack("<i", len(body)) + body


def _read_packet(sock: socket.socket) -> tuple[int, int, str]:
    length = struct.unpack("<i", _recv_exact(sock, 4))[0]
    if not 10 <= length <= 4 * 1024 * 1024:
        raise MinecraftControlError("Minecraft вернул повреждённый RCON-пакет.")
    body = _recv_exact(sock, length)
    request_id, packet_type = struct.unpack("<ii", body[:8])
    return request_id, packet_type, body[8:-2].decode("utf-8", errors="replace")


def minecraft_rcon(command: str, *, timeout_seconds: float | None = None) -> str:
    config = minecraft_config()
    if not config.configured:
        raise MinecraftControlError("Minecraft RCON ещё не настроен.")
    password = _read_password(config)
    timeout = config.timeout_seconds
    if timeout_seconds is not None:
        timeout = max(0.5, min(config.timeout_seconds, float(timeout_seconds)))
    try:
        with socket.create_connection(
            (config.host, config.port),
            timeout=timeout,
        ) as sock:
            sock.settimeout(timeout)
            sock.sendall(_packet(1001, 3, password))
            auth_id, auth_type, _ = _read_packet(sock)
            if auth_id == 1001 and auth_type == 0:
                auth_id, auth_type, _ = _read_packet(sock)
            if auth_id == -1:
                raise MinecraftControlError("Minecraft отклонил RCON-секрет.")
            if auth_id != 1001:
                raise MinecraftControlError("Minecraft вернул чужой ответ авторизации.")
            sock.sendall(_packet(1002, 2, str(command)))
            response_id, _, response = _read_packet(sock)
            if response_id != 1002:
                raise MinecraftControlError("Minecraft вернул чужой RCON-ответ.")
            return response.strip()
    except MinecraftControlError:
        raise
    except (OSError, socket.timeout) as exc:
        raise MinecraftControlError("Minecraft-сервер сейчас недоступен.") from exc


_LIST_PATTERN = re.compile(
    r"There are\s+(?P<online>\d+)\s+of a max of\s+(?P<maximum>\d+)\s+players online:?(?P<players>.*)",
    re.IGNORECASE,
)


def minecraft_status() -> dict[str, Any]:
    config = minecraft_config()
    if not config.configured:
        return {
            "configured": False,
            "online": False,
            "state": "disabled",
            "address": os.getenv("MINECRAFT_PUBLIC_ADDRESS", "mc.tvr.lat"),
        }
    lifecycle: dict[str, Any] = {}
    lifecycle_error = ""
    try:
        lifecycle = minecraft_supervisor("status")
    except MinecraftControlError as exc:
        lifecycle_error = str(exc)

    operation = lifecycle.get("operation")
    operation_active = isinstance(operation, dict) and operation.get("status") in {
        "queued",
        "running",
    }
    operation_failed = (
        isinstance(operation, dict) and operation.get("status") == "failed"
    )
    if lifecycle and not lifecycle.get("running"):
        state = (
            str(operation.get("state") or "changing")
            if operation_active and isinstance(operation, dict)
            else "error"
            if operation_failed
            else str(lifecycle.get("state") or "offline")
        )
        operation_error = (
            str(operation.get("error") or "неизвестная ошибка")[:300]
            if operation_failed and isinstance(operation, dict)
            else ""
        )
        return {
            "configured": True,
            "online": False,
            "state": state,
            "address": os.getenv("MINECRAFT_PUBLIC_ADDRESS", "mc.tvr.lat"),
            "error": (
                "Операция с сервером выполняется."
                if operation_active
                else f"Операция Minecraft не выполнена: {operation_error}"
                if operation_failed
                else "Minecraft-контейнер остановлен."
            ),
            "lifecycle": lifecycle,
        }
    try:
        raw = minecraft_rcon("list", timeout_seconds=1.5)
    except MinecraftControlError as exc:
        state = (
            str(operation.get("state") or "changing")
            if operation_active and isinstance(operation, dict)
            else str(lifecycle.get("state") or "offline")
        )
        return {
            "configured": True,
            "online": False,
            "state": state,
            "address": os.getenv("MINECRAFT_PUBLIC_ADDRESS", "mc.tvr.lat"),
            "error": str(exc) if not lifecycle_error else f"{exc} {lifecycle_error}",
            "lifecycle": lifecycle,
        }
    match = _LIST_PATTERN.search(raw)
    players = []
    online = 0
    maximum = 0
    if match:
        online = int(match.group("online"))
        maximum = int(match.group("maximum"))
        players = [
            value.strip()
            for value in match.group("players").split(",")
            if value.strip()
        ]
    return {
        "configured": True,
        "online": True,
        "state": "online",
        "address": os.getenv("MINECRAFT_PUBLIC_ADDRESS", "mc.tvr.lat"),
        "players_online": online,
        "players_max": maximum,
        "players": players,
        "raw": raw[:500],
        "lifecycle": lifecycle,
    }


def _clean_player(value: Any) -> str:
    player = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_]{3,16}", player):
        raise MinecraftControlError("Некорректное имя игрока.")
    return player


def _clean_console_command(value: Any) -> str:
    command = str(value or "").strip()
    if command.startswith("/"):
        command = command[1:].lstrip()
    if not 1 <= len(command) <= 500 or any(ord(char) < 32 for char in command):
        raise MinecraftControlError("Некорректная консольная команда.")
    command_name = command.split(maxsplit=1)[0].lower()
    if command_name == "stop" or command_name.endswith(":stop"):
        raise MinecraftControlError(
            "Для остановки используйте управляемую кнопку в Реакторе.",
        )
    return command


def minecraft_execute(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    selected = str(action or "").strip().lower()
    command: str
    if selected == "save":
        command = "save-all flush"
    elif selected == "announce":
        message = " ".join(str(payload.get("message") or "").split())[:180]
        if len(message) < 2:
            raise MinecraftControlError("Введите текст объявления.")
        command = f"say [T-Mod] {message}"
    elif selected == "kick":
        player = _clean_player(payload.get("player"))
        reason = " ".join(
            str(payload.get("reason") or "Решение администрации").split()
        )[:120]
        command = f"kick {player} {reason}"
    elif selected in {"whitelist_add", "whitelist_remove"}:
        player = _clean_player(payload.get("player"))
        command = (
            f"whitelist {'add' if selected.endswith('add') else 'remove'} {player}"
        )
    elif selected in {"whitelist_on", "whitelist_off"}:
        command = f"whitelist {'on' if selected.endswith('on') else 'off'}"
    elif selected in {"start", "stop", "restart"}:
        if selected in {"stop", "restart"}:
            try:
                minecraft_rcon(
                    "say [T-Mod] Сервер останавливается по команде администратора."
                )
                minecraft_rcon("save-all flush")
            except MinecraftControlError:
                pass
        lifecycle = minecraft_supervisor(selected)
        response = {
            "start": "Запуск Minecraft принят. Состояние обновится автоматически.",
            "stop": "Остановка Minecraft принята. Состояние обновится автоматически.",
            "restart": "Перезапуск Minecraft принят. Состояние обновится автоматически.",
        }[selected]
        return {
            "ok": True,
            "action": selected,
            "response": response[:1000] or "Команда принята.",
            "lifecycle": lifecycle,
        }
    elif selected == "console":
        command = _clean_console_command(payload.get("command"))
    else:
        raise MinecraftControlError("Эта команда Minecraft не разрешена.")
    response = minecraft_rcon(command)
    return {
        "ok": True,
        "action": selected,
        "response": response[:1000] or "Команда принята сервером.",
    }


__all__ = [
    "MinecraftConfig",
    "MinecraftControlError",
    "minecraft_config",
    "minecraft_execute",
    "minecraft_rcon",
    "minecraft_status",
    "minecraft_supervisor",
]
