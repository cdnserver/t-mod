import os
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from modules import minecraft_control as minecraft


def packet(request_id: int, packet_type: int, payload: str) -> bytes:
    encoded = payload.encode("utf-8")
    body = struct.pack("<ii", request_id, packet_type) + encoded + b"\x00\x00"
    return struct.pack("<i", len(body)) + body


class FakeSocket:
    def __init__(self, incoming: bytes) -> None:
        self.incoming = bytearray(incoming)
        self.sent = bytearray()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def settimeout(self, _):
        return None

    def sendall(self, value: bytes) -> None:
        self.sent.extend(value)

    def recv(self, size: int) -> bytes:
        value = bytes(self.incoming[:size])
        del self.incoming[:size]
        return value


class MinecraftControlTests(unittest.TestCase):
    def test_rcon_authenticates_and_sends_only_requested_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            secret = Path(directory) / "rcon.txt"
            secret.write_text("a" * 32, encoding="utf-8")
            config = minecraft.MinecraftConfig("minecraft", 25575, secret, 1.0)
            fake = FakeSocket(
                packet(1001, 0, "")
                + packet(1001, 2, "")
                + packet(1002, 0, "Saved the game")
            )
            with (
                patch(
                    "modules.minecraft_control.minecraft_config", return_value=config
                ),
                patch(
                    "modules.minecraft_control.socket.create_connection",
                    return_value=fake,
                ),
            ):
                response = minecraft.minecraft_rcon("save-all flush")
        self.assertEqual(response, "Saved the game")
        self.assertIn(b"save-all flush", bytes(fake.sent))
        self.assertNotIn(b"tmod-local-rcon", bytes(fake.sent))

    def test_status_parses_players_and_unconfigured_mode_is_safe(self) -> None:
        with (
            patch(
                "modules.minecraft_control.minecraft_rcon",
                return_value="There are 2 of a max of 20 players online: Alice, Bob",
            ),
            patch.object(
                minecraft.MinecraftConfig,
                "configured",
                new_callable=lambda: property(lambda _: True),
            ),
        ):
            status = minecraft.minecraft_status()
        self.assertTrue(status["online"])
        self.assertEqual(status["players"], ["Alice", "Bob"])

        with patch.dict(
            os.environ,
            {"MINECRAFT_RCON_PASSWORD_FILE": "/definitely/missing/tmod-secret"},
        ):
            unavailable = minecraft.minecraft_status()
        self.assertFalse(unavailable["configured"])
        self.assertFalse(unavailable["online"])

    def test_execute_uses_allowlist_and_validates_player_names(self) -> None:
        commands: list[str] = []

        def run(command: str) -> str:
            commands.append(command)
            return "OK"

        with patch("modules.minecraft_control.minecraft_rcon", side_effect=run):
            result = minecraft.minecraft_execute(
                "announce",
                {"message": "  Сервер   обновлён  "},
            )
            self.assertTrue(result["ok"])
            minecraft.minecraft_execute("whitelist_add", {"player": "Player_15"})
            console = minecraft.minecraft_execute(
                "console",
                {"command": "/time set day"},
            )
            self.assertTrue(console["ok"])
            with self.assertRaisesRegex(
                minecraft.MinecraftControlError,
                "не разрешена",
            ):
                minecraft.minecraft_execute("op", {"player": "Player_15"})
            with self.assertRaisesRegex(
                minecraft.MinecraftControlError,
                "имя игрока",
            ):
                minecraft.minecraft_execute("kick", {"player": "Player; op me"})
            with self.assertRaisesRegex(
                minecraft.MinecraftControlError,
                "управляемую кнопку",
            ):
                minecraft.minecraft_execute("console", {"command": "stop"})
        self.assertEqual(commands[0], "say [T-Mod] Сервер обновлён")
        self.assertEqual(commands[1], "whitelist add Player_15")
        self.assertEqual(commands[2], "time set day")

    def test_lifecycle_actions_use_narrow_supervisor_and_flush_before_stop(
        self,
    ) -> None:
        rcon_commands: list[str] = []
        lifecycle_actions: list[str] = []

        def rcon(command: str) -> str:
            rcon_commands.append(command)
            return "OK"

        def supervisor(action: str) -> dict[str, object]:
            lifecycle_actions.append(action)
            return {"ok": True, "state": "running" if action == "start" else "exited"}

        with (
            patch("modules.minecraft_control.minecraft_rcon", side_effect=rcon),
            patch(
                "modules.minecraft_control.minecraft_supervisor",
                side_effect=supervisor,
            ),
        ):
            started = minecraft.minecraft_execute("start", {})
            stopped = minecraft.minecraft_execute("stop", {})

        self.assertTrue(started["ok"])
        self.assertTrue(stopped["ok"])
        self.assertEqual(lifecycle_actions, ["start", "stop"])
        self.assertIn("save-all flush", rcon_commands)


if __name__ == "__main__":
    unittest.main()
