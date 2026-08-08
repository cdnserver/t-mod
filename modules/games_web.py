"""Authenticated multiplayer and bot board games for the personal Reactor."""

from __future__ import annotations

import asyncio
import json
import secrets
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Awaitable, Callable

import discord
from aiohttp import web

from modules.consensus_web_auth import ConsensusWebPrincipal, csrf_matches
from modules.games_engine import (
    GameRuleError,
    backgammon_bot_turn,
    backgammon_legal_moves,
    backgammon_move,
    chess_bot_move,
    chess_legal_moves,
    chess_move,
    new_backgammon_state,
    new_chess_state,
)
from persistence import game_repository as games


AuthenticatedRequest = Callable[
    [web.Request], Awaitable[tuple[ConsensusWebPrincipal | None, bool]]
]


def register_games_web_routes(
    app: web.Application,
    bot: discord.Client,
    *,
    guild_id: int,
    asset_dir: Path,
    authenticate: AuthenticatedRequest,
) -> None:
    rates: dict[int, deque[float]] = defaultdict(lambda: deque(maxlen=80))

    async def index(_: web.Request) -> web.FileResponse:
        return web.FileResponse(asset_dir / "games.html")

    async def principal(request: web.Request) -> ConsensusWebPrincipal:
        selected, legacy = await authenticate(request)
        if legacy or selected is None:
            raise web.HTTPUnauthorized(
                text=json.dumps({"error": "personal_login_required"}),
                content_type="application/json",
            )
        return selected

    async def body(request: web.Request, selected: ConsensusWebPrincipal) -> dict[str, Any]:
        if not csrf_matches(request, selected):
            raise web.HTTPForbidden(text='{"error":"csrf_failed"}', content_type="application/json")
        try:
            payload = await request.json()
        except (json.JSONDecodeError, TypeError):
            payload = None
        if not isinstance(payload, dict):
            raise web.HTTPBadRequest(text='{"error":"invalid_payload"}', content_type="application/json")
        now = asyncio.get_running_loop().time()
        bucket = rates[int(selected.user_id)]
        while bucket and now - bucket[0] > 60:
            bucket.popleft()
        if len(bucket) >= 60:
            raise web.HTTPTooManyRequests(text='{"error":"games_rate_limited"}', content_type="application/json")
        bucket.append(now)
        return payload

    def viewer(selected: ConsensusWebPrincipal) -> dict[str, Any]:
        return {
            "id": int(selected.user_id),
            "name": str(selected.display_name),
            "administrator": bool(selected.administrator),
            "csrf_token": str(selected.csrf_token),
        }

    def side_for(match: dict[str, Any], user_id: int) -> str | None:
        if int(match["host_user_id"]) == int(user_id):
            return str(match["host_side"])
        if match.get("guest_user_id") is not None and int(match["guest_user_id"]) == int(user_id):
            return "black" if str(match["host_side"]) == "white" else "white"
        return None

    def bot_side(match: dict[str, Any]) -> str:
        return "black" if str(match["host_side"]) == "white" else "white"

    def projection(match: dict[str, Any], selected: ConsensusWebPrincipal) -> dict[str, Any]:
        user_side = side_for(match, int(selected.user_id))
        legal: list[Any] = []
        can_move = bool(
            user_side
            and match["status"] == "active"
            and str(match["turn_side"]) == user_side
        )
        if can_move:
            legal = (
                chess_legal_moves(match["state"])
                if match["game_type"] == "chess"
                else backgammon_legal_moves(match["state"], user_side)
            )
        opposite = "black" if str(match["host_side"]) == "white" else "white"
        players = {
            str(match["host_side"]): {
                "id": int(match["host_user_id"]),
                "name": str(match["host_display"]),
                "bot": False,
            },
            opposite: {
                "id": int(match["guest_user_id"]) if match.get("guest_user_id") else None,
                "name": (
                    str(match.get("guest_display") or "Гость")
                    if match["mode"] == "friend"
                    else f"T‑Mod Bot · уровень {int(match['bot_level'])}"
                ),
                "bot": match["mode"] == "bot",
            },
        }
        return {
            **match,
            "players": players,
            "viewer_side": user_side,
            "spectator": user_side is None,
            "can_move": can_move,
            "can_join": bool(match["mode"] == "friend" and match["status"] == "waiting" and user_side is None),
            "legal_moves": legal,
            "invite_path": f"/games/{match['id']}",
        }

    def error_response(exc: Exception) -> web.Response:
        code = str(exc)
        status = 404 if code == "game_missing" else 409 if code == "game_version_conflict" else 400
        messages = {
            "game_missing": "Матч не найден.",
            "game_version_conflict": "Матч уже изменился. Обновляем позицию.",
            "game_not_joinable": "К этому матчу уже нельзя присоединиться.",
            "game_not_yours": "Вы наблюдаете за матчем и не можете сделать ход.",
            "game_not_active": "Матч ещё не начался или уже завершён.",
            "game_wrong_turn": "Сейчас ход другого игрока.",
            "chess_move_illegal": "Эта фигура не может так ходить.",
            "backgammon_move_illegal": "Этот ход не соответствует выпавшим костям.",
        }
        return web.json_response({"error": code, "message": messages.get(code, "Действие не выполнено.")}, status=status)

    async def lobby(request: web.Request) -> web.Response:
        selected = await principal(request)
        matches = await asyncio.to_thread(games.game_list_for_user, int(guild_id), int(selected.user_id))
        return web.json_response({"viewer": viewer(selected), "matches": [projection(item, selected) for item in matches]})

    async def create(request: web.Request) -> web.Response:
        selected = await principal(request)
        payload = await body(request, selected)
        game_type = str(payload.get("game_type") or "chess")
        mode = str(payload.get("mode") or "bot")
        requested_side = str(payload.get("side") or "random")
        if requested_side == "random":
            requested_side = secrets.choice(("white", "black"))
        state = new_chess_state() if game_type == "chess" else new_backgammon_state()
        try:
            match = await asyncio.to_thread(
                games.game_create,
                guild_id=int(guild_id),
                game_type=game_type,
                mode=mode,
                host_user_id=int(selected.user_id),
                host_display=str(selected.display_name),
                host_side=requested_side,
                bot_level=int(payload.get("bot_level") or 1),
                state=state,
            )
            if mode == "bot" and requested_side == "black":
                if game_type == "chess":
                    state, turn, status, result, winner = chess_bot_move(state, int(match["bot_level"]))
                else:
                    state, turn, status, result, winner = backgammon_bot_turn(state, "white", int(match["bot_level"]))
                match = await asyncio.to_thread(
                    games.game_update,
                    int(guild_id), match["id"], expected_version=int(match["version"]),
                    actor_user_id=None, action="bot_opening", state=state, turn_side=turn,
                    status=status, result=result, winner_side=winner,
                )
        except (games.GameStorageError, GameRuleError, TypeError, ValueError) as exc:
            return error_response(exc)
        return web.json_response({"match": projection(match, selected)}, status=201)

    async def detail(request: web.Request) -> web.Response:
        selected = await principal(request)
        match = await asyncio.to_thread(games.game_get, int(guild_id), request.match_info["match_id"])
        if match is None:
            return error_response(games.GameStorageError("game_missing"))
        return web.json_response({"viewer": viewer(selected), "match": projection(match, selected)})

    async def command(request: web.Request) -> web.Response:
        selected = await principal(request)
        payload = await body(request, selected)
        match_id = str(request.match_info["match_id"])
        action = str(payload.get("action") or "")
        try:
            if action == "join":
                joined = await asyncio.to_thread(
                    games.game_join, int(guild_id), match_id, int(selected.user_id), str(selected.display_name)
                )
                return web.json_response({"match": projection(joined, selected)})
            match = await asyncio.to_thread(games.game_get, int(guild_id), match_id)
            if match is None:
                raise games.GameStorageError("game_missing")
            user_side = side_for(match, int(selected.user_id))
            if user_side is None:
                raise games.GameStorageError("game_not_yours")
            if action == "resign":
                if match["status"] != "active":
                    raise games.GameStorageError("game_not_active")
                winner = "black" if user_side == "white" else "white"
                updated = await asyncio.to_thread(
                    games.game_update, int(guild_id), match_id,
                    expected_version=int(payload.get("version") or 0), actor_user_id=int(selected.user_id),
                    action="resigned", state=match["state"], turn_side=str(match["turn_side"]),
                    status="finished", result="resigned", winner_side=winner,
                )
                return web.json_response({"match": projection(updated, selected)})
            if action != "move":
                raise games.GameStorageError("game_action_invalid")
            if match["status"] != "active":
                raise games.GameStorageError("game_not_active")
            if str(match["turn_side"]) != user_side:
                raise games.GameStorageError("game_wrong_turn")
            if match["game_type"] == "chess":
                state, turn, status, result, winner = chess_move(match["state"], str(payload.get("move") or ""))
            else:
                state, turn, status, result, winner = backgammon_move(
                    match["state"], user_side, payload.get("from"), payload.get("to"),
                    int(payload["die"]) if payload.get("die") is not None else None,
                )
            if status == "active" and match["mode"] == "bot" and turn == bot_side(match):
                if match["game_type"] == "chess":
                    state, turn, status, result, winner = chess_bot_move(state, int(match["bot_level"]))
                else:
                    state, turn, status, result, winner = backgammon_bot_turn(state, turn, int(match["bot_level"]))
            updated = await asyncio.to_thread(
                games.game_update, int(guild_id), match_id,
                expected_version=int(payload.get("version") or 0), actor_user_id=int(selected.user_id),
                action="move", state=state, turn_side=turn, status=status,
                result=result, winner_side=winner, event={"move": payload.get("move"), "from": payload.get("from"), "to": payload.get("to")},
            )
            return web.json_response({"match": projection(updated, selected)})
        except (games.GameStorageError, GameRuleError, TypeError, ValueError) as exc:
            return error_response(exc)

    app.router.add_get("/games", index)
    app.router.add_get("/games/", index)
    app.router.add_get("/games/{match_id}", index)
    app.router.add_get("/api/games/lobby", lobby)
    app.router.add_post("/api/games/matches", create)
    app.router.add_get("/api/games/matches/{match_id}", detail)
    app.router.add_post("/api/games/matches/{match_id}/command", command)


__all__ = ["register_games_web_routes"]
