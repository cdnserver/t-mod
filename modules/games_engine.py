"""Rules and lightweight bot opponents for Reactor chess and backgammon."""

from __future__ import annotations

import random
from typing import Any

import chess


class GameRuleError(RuntimeError):
    pass


_RANDOM = random.SystemRandom()
_PIECE_VALUES = {
    chess.PAWN: 100,
    chess.KNIGHT: 320,
    chess.BISHOP: 330,
    chess.ROOK: 500,
    chess.QUEEN: 900,
    chess.KING: 0,
}


def new_chess_state() -> dict[str, Any]:
    return {"fen": chess.STARTING_FEN, "last_move": None, "history": []}


def _chess_result(board: chess.Board) -> tuple[str, str | None, str | None]:
    if not board.is_game_over(claim_draw=True):
        return "active", None, None
    outcome = board.outcome(claim_draw=True)
    if outcome is None or outcome.winner is None:
        return "finished", "draw", None
    winner = "white" if outcome.winner == chess.WHITE else "black"
    return "finished", "checkmate", winner


def chess_move(state: dict[str, Any], uci: str) -> tuple[dict[str, Any], str, str, str | None, str | None]:
    board = chess.Board(str(state.get("fen") or chess.STARTING_FEN))
    try:
        move = chess.Move.from_uci(str(uci or "").strip().lower())
    except ValueError as exc:
        raise GameRuleError("chess_move_invalid") from exc
    if move not in board.legal_moves:
        raise GameRuleError("chess_move_illegal")
    san = board.san(move)
    board.push(move)
    history = list(state.get("history") or [])[-119:]
    history.append(san)
    updated = {"fen": board.fen(), "last_move": move.uci(), "history": history}
    status, result, winner = _chess_result(board)
    turn = "white" if board.turn == chess.WHITE else "black"
    return updated, turn, status, result, winner


def chess_legal_moves(state: dict[str, Any]) -> list[str]:
    board = chess.Board(str(state.get("fen") or chess.STARTING_FEN))
    return [move.uci() for move in board.legal_moves]


def chess_bot_move(state: dict[str, Any], level: int) -> tuple[dict[str, Any], str, str, str | None, str | None]:
    board = chess.Board(str(state.get("fen") or chess.STARTING_FEN))
    legal = list(board.legal_moves)
    if not legal:
        status, result, winner = _chess_result(board)
        return state, "white" if board.turn == chess.WHITE else "black", status, result, winner
    selected_level = max(1, min(3, int(level)))
    if selected_level == 1:
        move = _RANDOM.choice(legal)
    else:
        scored: list[tuple[float, chess.Move]] = []
        for candidate in legal:
            captured = board.piece_at(candidate.to_square)
            score = float(_PIECE_VALUES.get(captured.piece_type, 0) if captured else 0)
            if candidate.promotion:
                score += _PIECE_VALUES.get(candidate.promotion, 0) - 100
            board.push(candidate)
            if board.is_checkmate():
                score += 100000
            elif board.is_check():
                score += 45
            if selected_level >= 3:
                replies = list(board.legal_moves)
                if replies:
                    worst_reply = max(
                        (_PIECE_VALUES.get(board.piece_at(reply.to_square).piece_type, 0)
                         if board.piece_at(reply.to_square) else 0)
                        for reply in replies
                    )
                    score -= worst_reply * 0.72
                score += len(replies) * -0.08
            board.pop()
            score += _RANDOM.random() * (55 if selected_level == 2 else 8)
            scored.append((score, candidate))
        move = max(scored, key=lambda item: item[0])[1]
    return chess_move(state, move.uci())


def _roll() -> list[int]:
    first, second = _RANDOM.randint(1, 6), _RANDOM.randint(1, 6)
    return [first] * 4 if first == second else [first, second]


def new_backgammon_state() -> dict[str, Any]:
    board = [0] * 24
    board[0], board[11], board[16], board[18] = 2, 5, 3, 5
    board[23], board[12], board[7], board[5] = -2, -5, -3, -5
    return {
        "board": board,
        "bar": {"white": 0, "black": 0},
        "off": {"white": 0, "black": 0},
        "dice": _roll(),
        "last_move": None,
        "history": [],
    }


def _sign(side: str) -> int:
    return 1 if side == "white" else -1


def _home_ready(state: dict[str, Any], side: str) -> bool:
    board, sign = state["board"], _sign(side)
    if int(state["bar"][side]) > 0:
        return False
    outside = range(0, 18) if side == "white" else range(6, 24)
    return not any(int(board[index]) * sign > 0 for index in outside)


def backgammon_legal_moves(state: dict[str, Any], side: str) -> list[dict[str, Any]]:
    board = [int(value) for value in state.get("board") or []]
    if len(board) != 24:
        raise GameRuleError("backgammon_state_invalid")
    sign = _sign(side)
    opponent = -sign
    moves: list[dict[str, Any]] = []
    dice = sorted({int(value) for value in state.get("dice") or []})
    for die in dice:
        if int(state["bar"][side]) > 0:
            destination = die - 1 if side == "white" else 24 - die
            if board[destination] * opponent < 2:
                moves.append({"from": "bar", "to": destination, "die": die})
            continue
        for source, value in enumerate(board):
            if value * sign <= 0:
                continue
            destination = source + die * sign
            if 0 <= destination < 24:
                if board[destination] * opponent < 2:
                    moves.append({"from": source, "to": destination, "die": die})
                continue
            if not _home_ready(state, side):
                continue
            exact = destination == 24 if side == "white" else destination == -1
            behind = (
                any(board[index] * sign > 0 for index in range(18, source))
                if side == "white"
                else any(board[index] * sign > 0 for index in range(source + 1, 6))
            )
            if exact or not behind:
                moves.append({"from": source, "to": "off", "die": die})
    return moves


def _switch_backgammon_turn(state: dict[str, Any], side: str) -> tuple[dict[str, Any], str]:
    next_side = "black" if side == "white" else "white"
    state["dice"] = _roll()
    return state, next_side


def backgammon_move(
    state: dict[str, Any],
    side: str,
    source: int | str,
    destination: int | str,
    die: int | None = None,
) -> tuple[dict[str, Any], str, str, str | None, str | None]:
    legal = backgammon_legal_moves(state, side)
    candidates = [
        move for move in legal
        if str(move["from"]) == str(source)
        and str(move["to"]) == str(destination)
        and (die is None or int(move["die"]) == int(die))
    ]
    if not candidates:
        raise GameRuleError("backgammon_move_illegal")
    move = sorted(candidates, key=lambda item: int(item["die"]))[0]
    updated = {
        **state,
        "board": [int(value) for value in state["board"]],
        "bar": {**state["bar"]},
        "off": {**state["off"]},
        "dice": [int(value) for value in state["dice"]],
        "history": list(state.get("history") or [])[-79:],
    }
    sign = _sign(side)
    if move["from"] == "bar":
        updated["bar"][side] -= 1
    else:
        updated["board"][int(move["from"])] -= sign
    if move["to"] == "off":
        updated["off"][side] += 1
    else:
        target = int(move["to"])
        if updated["board"][target] == -sign:
            updated["board"][target] = 0
            opponent = "black" if side == "white" else "white"
            updated["bar"][opponent] += 1
        updated["board"][target] += sign
    updated["dice"].remove(int(move["die"]))
    updated["last_move"] = move
    updated["history"].append({"side": side, **move})
    if int(updated["off"][side]) >= 15:
        return updated, side, "finished", "bear_off", side
    if not updated["dice"] or not backgammon_legal_moves(updated, side):
        updated, next_side = _switch_backgammon_turn(updated, side)
        return updated, next_side, "active", None, None
    return updated, side, "active", None, None


def backgammon_bot_turn(
    state: dict[str, Any], side: str, level: int
) -> tuple[dict[str, Any], str, str, str | None, str | None]:
    current = state
    turn = side
    for _ in range(4):
        legal = backgammon_legal_moves(current, turn)
        if not legal:
            current, turn = _switch_backgammon_turn(current, turn)
            return current, turn, "active", None, None
        selected_level = max(1, min(3, int(level)))
        if selected_level == 1:
            move = _RANDOM.choice(legal)
        else:
            def score(item: dict[str, Any]) -> float:
                target = item["to"]
                points = float(item["die"])
                if target == "off":
                    points += 100
                elif current["board"][int(target)] == -_sign(turn):
                    points += 45
                elif abs(current["board"][int(target)]) == 1:
                    points += 8
                if selected_level == 3 and item["from"] != "bar":
                    points += int(item["from"]) * _sign(turn) * 0.15
                return points + _RANDOM.random() * (12 if selected_level == 2 else 2)
            move = max(legal, key=score)
        current, turn, status, result, winner = backgammon_move(
            current, turn, move["from"], move["to"], int(move["die"])
        )
        if status == "finished" or turn != side:
            return current, turn, status, result, winner
    return current, turn, "active", None, None


__all__ = [
    "GameRuleError",
    "backgammon_bot_turn",
    "backgammon_legal_moves",
    "backgammon_move",
    "chess_bot_move",
    "chess_legal_moves",
    "chess_move",
    "new_backgammon_state",
    "new_chess_state",
]
