import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "tmod_minecraft_supervisor",
    ROOT / "minecraft-supervisor" / "supervisor.py",
)
assert SPEC is not None and SPEC.loader is not None
supervisor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(supervisor)


class MinecraftSupervisorTests(unittest.TestCase):
    def test_status_exposes_only_lifecycle_metadata(self) -> None:
        payload = {
            "State": {
                "Status": "running",
                "Running": True,
                "StartedAt": "2026-08-01T12:00:00Z",
                "FinishedAt": "",
            },
            "Config": {"Env": ["SECRET=must-not-leak"]},
        }
        with patch.object(supervisor, "docker_request", return_value=(200, payload)):
            result = supervisor.container_status()

        self.assertTrue(result["running"])
        self.assertEqual(result["state"], "running")
        self.assertNotIn("Config", result)
        self.assertNotIn("SECRET", str(result))

    def test_lifecycle_is_hard_limited_to_bundled_container(self) -> None:
        calls: list[tuple[str, str]] = []

        def request(method: str, path: str):
            calls.append((method, path))
            if method == "GET":
                return 200, {"State": {"Status": "running", "Running": True}}
            return 204, None

        with patch.object(supervisor, "docker_request", side_effect=request):
            result = supervisor.lifecycle("restart")

        self.assertTrue(result["ok"])
        self.assertEqual(calls[0], ("POST", "/containers/minecraft/restart?t=30"))
        with self.assertRaisesRegex(supervisor.SupervisorError, "not allowed"):
            supervisor.lifecycle("delete")

    def test_token_must_be_present_and_sufficiently_long(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / "token"
            token.write_text("x" * 64, encoding="utf-8")
            with patch.object(supervisor, "TOKEN_FILE", token):
                self.assertEqual(supervisor.read_token(), "x" * 64)
            token.write_text("short", encoding="utf-8")
            with (
                patch.object(supervisor, "TOKEN_FILE", token),
                self.assertRaisesRegex(supervisor.SupervisorError, "invalid length"),
            ):
                supervisor.read_token()


if __name__ == "__main__":
    unittest.main()
