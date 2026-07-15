import os
import unittest
from unittest.mock import patch

from modules.majestic_api import (
    MajesticApiClient,
    MajesticApiConfig,
    MajesticApiDisabledError,
    MajesticApiRateLimitError,
)


class FakeResponse:
    def __init__(self, status_code: int, payload, *, headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.text = str(payload)

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, *responses: FakeResponse) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def request(self, method: str, url: str, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.responses.pop(0)

    def close(self) -> None:
        pass


def enabled_config(*, keys: tuple[str, ...] = ("super-secret",), cache_ttl: int = 60) -> MajesticApiConfig:
    return MajesticApiConfig(
        enabled=True,
        api_keys=keys,
        cache_ttl_seconds=cache_ttl,
        max_retries=0,
    )


class MajesticApiConfigTests(unittest.TestCase):
    def test_environment_parses_keys_without_exposing_them_in_repr(self) -> None:
        env = {
            "MAJESTIC_API_ENABLED": "true",
            "MAJESTIC_API_KEY": "primary-key",
            "MAJESTIC_API_KEYS": "backup-one; backup-two,primary-key",
        }
        with patch.dict(os.environ, env, clear=False):
            config = MajesticApiConfig.from_env()
        self.assertEqual(config.api_keys, ("primary-key", "backup-one", "backup-two"))
        self.assertNotIn("primary-key", repr(config))
        self.assertEqual(config.safe_summary()["configured_key_count"], 3)
        self.assertEqual(config.safe_summary()["key_pool_mode"], "primary-only")

    def test_disabled_client_never_sends_request(self) -> None:
        session = FakeSession(FakeResponse(200, {"ok": True}))
        client = MajesticApiClient(MajesticApiConfig(), session=session)
        with self.assertRaises(MajesticApiDisabledError):
            client.marketplace("items", 1)
        self.assertEqual(session.calls, [])


class MajesticApiClientTests(unittest.TestCase):
    def test_marketplace_request_uses_required_headers_and_cache(self) -> None:
        session = FakeSession(FakeResponse(200, {"items": [1, 2, 3]}))
        client = MajesticApiClient(enabled_config(), session=session)

        first = client.marketplace("items", 7)
        first["items"].append(999)
        second = client.marketplace("items", 7)

        self.assertEqual(second, {"items": [1, 2, 3]})
        self.assertEqual(len(session.calls), 1)
        call = session.calls[0]
        self.assertEqual(call["method"], "GET")
        self.assertEqual(call["url"], "https://api.majestic-files.net/v1/ext/marketplace/items/7")
        self.assertEqual(call["headers"]["x-api-key"], "super-secret")
        self.assertEqual(call["headers"]["x-language"], "ru")

    def test_429_preserves_retry_after_without_leaking_key(self) -> None:
        session = FakeSession(
            FakeResponse(
                429,
                {"errorDescription": "TOO_MANY_REQUESTS"},
                headers={"Retry-After": "12"},
            )
        )
        client = MajesticApiClient(enabled_config(), session=session)
        with self.assertRaises(MajesticApiRateLimitError) as caught:
            client.marketplace("vehicles", 3, use_cache=False)
        self.assertEqual(caught.exception.retry_after_seconds, 12)
        self.assertNotIn("super-secret", str(caught.exception))

    def test_only_primary_key_is_used_and_limit_is_shared(self) -> None:
        session = FakeSession(FakeResponse(200, {"ok": True}))
        client = MajesticApiClient(
            enabled_config(keys=("primary", "backup"), cache_ttl=0),
            session=session,
        )
        client.marketplace("houses", 2)
        self.assertEqual(session.calls[0]["headers"]["x-api-key"], "primary")
        diagnostics = client.diagnostics()
        self.assertEqual(diagnostics["configured_key_count"], 2)
        self.assertEqual(diagnostics["remaining_process_budget"], 4)

    def test_unknown_marketplace_category_is_rejected_before_http(self) -> None:
        session = FakeSession(FakeResponse(200, {}))
        client = MajesticApiClient(enabled_config(), session=session)
        with self.assertRaises(ValueError):
            client.marketplace("unknown", 1)
        self.assertEqual(session.calls, [])


if __name__ == "__main__":
    unittest.main()
