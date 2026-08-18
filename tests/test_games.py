import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import chess
import storage
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from modules.consensus_web_auth import ConsensusWebPrincipal
from modules.games_engine import (
    GameRuleError,
    backgammon_legal_moves,
    backgammon_move,
    chess_bot_move,
    chess_legal_moves,
    chess_move,
    new_backgammon_state,
    new_chess_state,
)
from modules.games_web import register_games_web_routes
from persistence import game_repository as games


class GameEngineTests(unittest.TestCase):
    def test_chess_uses_complete_legal_move_rules(self) -> None:
        state = new_chess_state()
        self.assertIn("e2e4", chess_legal_moves(state))
        updated, turn, status, _, _ = chess_move(state, "e2e4")
        self.assertEqual(turn, "black")
        self.assertEqual(status, "active")
        self.assertEqual(chess.Board(updated["fen"]).piece_at(chess.E4).symbol(), "P")
        with self.assertRaisesRegex(GameRuleError, "chess_move_illegal"):
            chess_move(updated, "e2e5")

    def test_chess_bot_always_returns_a_valid_position(self) -> None:
        state, *_ = chess_move(new_chess_state(), "e2e4")
        updated, turn, status, _, _ = chess_bot_move(state, 3)
        board = chess.Board(updated["fen"])
        self.assertTrue(board.is_valid())
        self.assertEqual(turn, "white")
        self.assertEqual(status, "active")

    def test_chess_promotion_keeps_all_legal_choices(self) -> None:
        state = {
            "fen": "7k/P7/8/8/8/8/8/7K w - - 0 1",
            "last_move": None,
            "history": [],
        }
        choices = {move for move in chess_legal_moves(state) if move.startswith("a7a8")}
        self.assertEqual(choices, {"a7a8q", "a7a8r", "a7a8b", "a7a8n"})
        promoted, *_ = chess_move(state, "a7a8n")
        self.assertEqual(chess.Board(promoted["fen"]).piece_at(chess.A8).symbol(), "N")

    def test_backgammon_move_consumes_a_die_and_preserves_checkers(self) -> None:
        state = new_backgammon_state()
        legal = backgammon_legal_moves(state, "white")
        self.assertTrue(legal)
        move = legal[0]
        updated, turn, status, _, _ = backgammon_move(
            state, "white", move["from"], move["to"], move["die"]
        )
        total = sum(value for value in updated["board"] if value > 0) + updated["bar"]["white"] + updated["off"]["white"]
        self.assertEqual(total, 15)
        self.assertEqual(status, "active")
        self.assertIn(turn, {"white", "black"})

    def test_backgammon_forces_bar_entry(self) -> None:
        state = new_backgammon_state()
        state["board"][0] -= 1
        state["bar"]["white"] = 1
        state["dice"] = [1, 2]
        legal = backgammon_legal_moves(state, "white")
        self.assertTrue(legal)
        self.assertEqual({move["from"] for move in legal}, {"bar"})


class GameRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_data_dir = storage.DATA_DIR
        self.old_database_file = storage.DATABASE_FILE
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(self.temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "games-test.db"
        storage.init_db()

    def tearDown(self) -> None:
        storage.DATA_DIR = self.old_data_dir
        storage.DATABASE_FILE = self.old_database_file
        self.temp_dir.cleanup()

    def test_friend_match_join_and_version_conflict_are_safe(self) -> None:
        match = games.game_create(
            guild_id=1,
            game_type="chess",
            mode="friend",
            host_user_id=10,
            host_display="Первый",
            host_side="white",
            bot_level=1,
            state=new_chess_state(),
        )
        joined = games.game_join(1, match["id"], 20, "Второй")
        self.assertEqual(joined["status"], "active")
        self.assertEqual(joined["guest_user_id"], 20)
        state, turn, status, result, winner = chess_move(joined["state"], "e2e4")
        saved = games.game_update(
            1,
            match["id"],
            expected_version=joined["version"],
            actor_user_id=10,
            action="move",
            state=state,
            turn_side=turn,
            status=status,
            result=result,
            winner_side=winner,
        )
        self.assertEqual(saved["version"], joined["version"] + 1)
        with self.assertRaisesRegex(games.GameStorageError, "game_version_conflict"):
            games.game_update(
                1,
                match["id"],
                expected_version=joined["version"],
                actor_user_id=10,
                action="duplicate",
                state=state,
                turn_side=turn,
                status=status,
            )

    def test_matches_are_isolated_by_guild(self) -> None:
        match = games.game_create(
            guild_id=1,
            game_type="backgammon",
            mode="bot",
            host_user_id=10,
            host_display="Игрок",
            host_side="white",
            bot_level=2,
            state=new_backgammon_state(),
        )
        self.assertIsNotNone(games.game_get(1, match["id"]))
        self.assertIsNone(games.game_get(2, match["id"]))

    def test_chess_message_is_private_and_rate_limited(self) -> None:
        match = games.game_create(
            guild_id=1,
            game_type="chess",
            mode="friend",
            host_user_id=10,
            host_display="Первый",
            host_side="white",
            bot_level=1,
            state=new_chess_state(),
        )
        games.game_join(1, match["id"], 20, "Второй")
        sent = games.game_send_chess_message(
            1,
            match["id"],
            actor_user_id=10,
            recipient_user_id=20,
            sender_display="Первый",
            message="  Шах и мат через три хода!  ",
        )

        sender_view = games.game_list_chess_messages_for_user(1, match["id"], 10)
        recipient_view = games.game_list_chess_messages_for_user(1, match["id"], 20)
        self.assertEqual(sender_view["messages"], [])
        self.assertEqual(recipient_view["messages"][0]["message"], "Шах и мат через три хода!")
        self.assertEqual(recipient_view["messages"][0]["recipient_side"], "black")
        self.assertEqual(recipient_view["cursor"], sent["id"])

        with self.assertRaisesRegex(games.GameStorageError, "game_message_rate_limited"):
            games.game_send_chess_message(
                1,
                match["id"],
                actor_user_id=10,
                recipient_user_id=20,
                sender_display="Первый",
                message="Ещё раз",
            )

    def test_chess_message_rejects_outsiders_and_wrong_target(self) -> None:
        match = games.game_create(
            guild_id=1,
            game_type="chess",
            mode="friend",
            host_user_id=10,
            host_display="Первый",
            host_side="black",
            bot_level=1,
            state=new_chess_state(),
        )
        games.game_join(1, match["id"], 20, "Второй")
        with self.assertRaisesRegex(games.GameStorageError, "game_not_yours"):
            games.game_send_chess_message(
                1,
                match["id"],
                actor_user_id=30,
                recipient_user_id=20,
                sender_display="Зритель",
                message="Помеха",
            )
        with self.assertRaisesRegex(games.GameStorageError, "game_message_target_invalid"):
            games.game_send_chess_message(
                1,
                match["id"],
                actor_user_id=10,
                recipient_user_id=30,
                sender_display="Первый",
                message="Не туда",
            )


class GameWebTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.old_data_dir = storage.DATA_DIR
        self.old_database_file = storage.DATABASE_FILE
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(self.temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "games-web-test.db"
        storage.init_db()

    def tearDown(self) -> None:
        storage.DATA_DIR = self.old_data_dir
        storage.DATABASE_FILE = self.old_database_file
        self.temp_dir.cleanup()

    async def test_chess_bot_match_create_and_move(self) -> None:
        member = SimpleNamespace(
            id=10,
            display_name="Игрок",
            guild_permissions=SimpleNamespace(administrator=False),
            roles=[],
        )
        selected = ConsensusWebPrincipal(
            user_id=10,
            guild_id=1,
            display_name="Игрок",
            csrf_token="games-csrf",
            member=member,
        )

        async def authenticate(_request):
            return selected, False

        app = web.Application()
        register_games_web_routes(
            app,
            SimpleNamespace(),
            guild_id=1,
            asset_dir=Path(__file__).resolve().parents[1] / "web" / "consensus",
            authenticate=authenticate,
        )
        headers = {"X-CSRF-Token": "games-csrf"}
        async with TestClient(TestServer(app)) as client:
            created_response = await client.post(
                "/api/games/matches",
                json={"game_type": "chess", "mode": "bot", "side": "white", "bot_level": 2},
                headers=headers,
            )
            created = (await created_response.json())["match"]
            moved_response = await client.post(
                f"/api/games/matches/{created['id']}/command",
                json={"action": "move", "version": created["version"], "move": "e2e4"},
                headers=headers,
            )
            moved = (await moved_response.json())["match"]

        self.assertEqual(created_response.status, 201)
        self.assertEqual(moved_response.status, 200)
        self.assertEqual(moved["turn_side"], "white")
        self.assertTrue(moved["can_move"])
        self.assertEqual(len(moved["state"]["history"]), 2)

    async def test_friend_invite_join_and_spectator_projection(self) -> None:
        principals = {}
        for user_id, name in ((10, "Автор"), (20, "Гость"), (30, "Зритель")):
            member = SimpleNamespace(
                id=user_id,
                display_name=name,
                guild_permissions=SimpleNamespace(administrator=False),
                roles=[],
            )
            principals[user_id] = ConsensusWebPrincipal(
                user_id=user_id,
                guild_id=1,
                display_name=name,
                csrf_token=f"csrf-{user_id}",
                member=member,
            )

        async def authenticate(request):
            return principals[int(request.headers["X-Test-User"])], False

        app = web.Application()
        register_games_web_routes(
            app,
            SimpleNamespace(),
            guild_id=1,
            asset_dir=Path(__file__).resolve().parents[1] / "web" / "consensus",
            authenticate=authenticate,
        )
        async with TestClient(TestServer(app)) as client:
            created_response = await client.post(
                "/api/games/matches",
                json={"game_type": "chess", "mode": "friend", "side": "white"},
                headers={"X-Test-User": "10", "X-CSRF-Token": "csrf-10"},
            )
            created = (await created_response.json())["match"]
            invite_response = await client.get(
                f"/api/games/matches/{created['id']}",
                headers={"X-Test-User": "20"},
            )
            invite = (await invite_response.json())["match"]
            joined_response = await client.post(
                f"/api/games/matches/{created['id']}/command",
                json={"action": "join"},
                headers={"X-Test-User": "20", "X-CSRF-Token": "csrf-20"},
            )
            joined = (await joined_response.json())["match"]
            await asyncio.to_thread(
                games.game_send_chess_message,
                1,
                created["id"],
                actor_user_id=10,
                recipient_user_id=20,
                sender_display="Автор",
                message="Твой король под наблюдением",
            )
            sender_messages_response = await client.get(
                f"/api/games/matches/{created['id']}/messages",
                headers={"X-Test-User": "10"},
            )
            recipient_messages_response = await client.get(
                f"/api/games/matches/{created['id']}/messages",
                headers={"X-Test-User": "20"},
            )
            sender_messages = await sender_messages_response.json()
            recipient_messages = await recipient_messages_response.json()
            spectator_response = await client.get(
                f"/api/games/matches/{created['id']}",
                headers={"X-Test-User": "30"},
            )
            spectator = (await spectator_response.json())["match"]

        self.assertEqual(created_response.status, 201)
        self.assertTrue(invite["spectator"])
        self.assertTrue(invite["can_join"])
        self.assertEqual(joined_response.status, 200)
        self.assertEqual(joined["viewer_side"], "black")
        self.assertTrue(joined["can_move"] is False)
        self.assertEqual(sender_messages["messages"], [])
        self.assertEqual(
            recipient_messages["messages"][0]["message"],
            "Твой король под наблюдением",
        )
        self.assertTrue(spectator["spectator"])
        self.assertFalse(spectator["can_join"])
        self.assertEqual(spectator["legal_moves"], [])


if __name__ == "__main__":
    unittest.main()
