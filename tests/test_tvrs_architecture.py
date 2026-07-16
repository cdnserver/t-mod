import ast
import asyncio
import json
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from modules import tvrs
from modules.consensus_core import ConsensusRules, LiveConsensusSession, LiveParticipant, LiveResult
from modules.consensus_runtime import active_consensus_snapshot, active_sessions, registry
from modules.delivery_outbox import OutboxMessage
from modules.market_domain import market_internal_id, rank_market_items
from modules.hub_runtime import open_hub_section, register_hub_section
from modules.operations_runtime import bind_worker_wakeup, wake_operations_worker
from modules.tvrs_config import env_color, env_int
from modules.tvrs_delivery import (
    TVRS_CONTROL_DM_TOPIC,
    TVRS_RETRY_BILL_TOPIC,
    TVRS_SESSION_SUMMARY_TOPIC,
    build_control_dm_deliveries,
    build_retry_bill_delivery,
    build_result_deliveries,
    build_session_summary_deliveries,
    make_result_delivery_handler,
    make_retry_bill_delivery_handler,
)
from modules.tvrs_embeds import build_final_summary_embed, build_result_embed
from modules.tvrs_formatting import (
    clip_text,
    format_bill_number,
    format_timer,
    materials_text,
    progress_bar,
    result_status_text,
)


ROOT = Path(__file__).resolve().parents[1]


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


def make_session() -> LiveConsensusSession:
    participants = {
        1: LiveParticipant(1, "Ведущий", "<@1>", "chair", permanent=True, confirmed=True),
        2: LiveParticipant(2, "Председатель", "<@2>", "chair", confirmed=True),
        3: LiveParticipant(3, "Сенатор", "<@3>", "senator", confirmed=True),
    }
    return LiveConsensusSession(
        session_key="77:architecture",
        guild_id=77,
        channel_id=100,
        leader_id=1,
        leader_display="Ведущий",
        plenary_number=4,
        participants=participants,
    )


class ArchitectureBoundaryTests(unittest.TestCase):
    def test_domain_and_application_layers_do_not_import_infrastructure(self) -> None:
        forbidden = {"discord", "storage", "requests", "os"}
        for relative_path in (
            "modules/consensus_core.py",
            "modules/consensus_service.py",
            "modules/control_center_runtime.py",
            "modules/craft_runtime.py",
            "modules/finance_formatting.py",
            "modules/finance_runtime.py",
            "modules/hub_runtime.py",
            "modules/market_domain.py",
            "modules/operations_runtime.py",
            "modules/public_panel_runtime.py",
            "modules/tvrs_formatting.py",
            "modules/tvrs_navigation_runtime.py",
        ):
            imports = imported_modules(ROOT / relative_path)
            violations = {
                module
                for module in imports
                if any(module == item or module.startswith(f"{item}.") for item in forbidden)
            }
            self.assertEqual(violations, set(), f"{relative_path} crossed a clean-layer boundary")

    def test_operations_reads_runtime_without_importing_tvrs_ui(self) -> None:
        imports = imported_modules(ROOT / "modules/operations.py")
        self.assertNotIn("modules.tvrs", imports)

    def test_legacy_discord_modules_do_not_recreate_reverse_dependency_edges(self) -> None:
        forbidden_edges = {
            "modules/control_center.py": {"modules.operations", "modules.tvrs"},
            "modules/operations.py": {"modules.tvrs"},
            "modules/finance.py": {
                "modules.control_center",
                "modules.craft",
                "modules.operations",
                "modules.tvrs",
            },
            "modules/craft.py": {
                "modules.control_center",
                "modules.finance",
                "modules.operations",
                "modules.tvrs",
            },
            "modules/market.py": {"modules.control_center", "modules.tvrs"},
            "modules/tvrs.py": {
                "modules.control_center",
                "modules.craft",
                "modules.finance",
                "modules.market",
                "modules.operations",
            },
        }
        for relative_path, forbidden in forbidden_edges.items():
            imports = imported_modules(ROOT / relative_path)
            violations = {
                module
                for module in imports
                if any(module == item or module.startswith(f"{item}.") for item in forbidden)
            }
            self.assertEqual(violations, set(), f"{relative_path} recreated a legacy dependency cycle")

    def test_legacy_tvrs_facade_keeps_public_symbols(self) -> None:
        from modules import consensus_runtime, tvrs_embeds, tvrs_formatting

        symbols = {
            "active_consensus_snapshot": consensus_runtime.active_consensus_snapshot,
            "format_bill_number": tvrs_formatting.format_bill_number,
            "format_timer": tvrs_formatting.format_timer,
            "build_result_embed": tvrs_embeds.build_result_embed,
            "build_final_summary_embed": tvrs_embeds.build_final_summary_embed,
        }
        for name, expected in symbols.items():
            self.assertIs(getattr(tvrs, name), expected)

    def test_legacy_market_facade_keeps_domain_symbols(self) -> None:
        from modules import market, market_domain

        symbols = {
            "MarketSearchHit": market_domain.MarketSearchHit,
            "normalize_market_text": market_domain.normalize_market_text,
            "_market_internal_id": market_domain.market_internal_id,
            "_clean_market_category": market_domain.clean_market_category,
        }
        for name, expected in symbols.items():
            self.assertIs(getattr(market, name), expected)

    def test_major_discord_modules_import_in_clean_processes_in_both_directions(self) -> None:
        orders = (
            "modules.tvrs, modules.finance, modules.craft, modules.market, modules.operations, modules.control_center",
            "modules.control_center, modules.operations, modules.market, modules.craft, modules.finance, modules.tvrs",
        )
        for order in orders:
            completed = subprocess.run(
                [sys.executable, "-c", f"import {order}"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)


class TVRSFormattingContractTests(unittest.TestCase):
    def test_config_parsers_are_total_and_have_safe_fallbacks(self) -> None:
        self.assertEqual(env_int("T_MOD_TEST_MISSING_INT", 17), 17)
        self.assertEqual(env_color("T_MOD_TEST_MISSING_COLOR", "0xABCDEF"), 0xABCDEF)

    def test_formatting_boundaries(self) -> None:
        self.assertEqual(format_bill_number(9), "009")
        self.assertEqual(format_timer(None), "не установлен")
        self.assertEqual(format_timer(90), "1 мин. 30 сек.")
        self.assertEqual(materials_text("железо, ткань"), "• железо\n• ткань")
        self.assertEqual(clip_text("abcdef", 4), "abc…")
        self.assertEqual(progress_bar(-1, 4), "░░░░")
        self.assertEqual(progress_bar(101, 4), "████")
        self.assertEqual(result_status_text("accepted"), "принят")

    def test_result_embed_uses_rules_frozen_for_the_session(self) -> None:
        session = make_session()
        session.rules = ConsensusRules(acceptance_percent=60.0)
        result = LiveResult(
            bill_id=10,
            bill_number=9,
            title="Архитектурный контракт",
            status="accepted",
            internal_percent=100.0,
            overall_percent=60.0,
            internal_active=True,
            votes={1: "yes", 2: "no", 3: "yes"},
        )

        payload = build_result_embed(result, session).to_dict()
        self.assertEqual(payload["title"], "✅ Итог голосования • №009")
        fields = {field["name"]: field["value"] for field in payload["fields"]}
        self.assertIn("порог принятия `60.0%`", fields["🌐 Общий консенсус"])
        self.assertIn("<@1>", fields["🧾 Зафиксированные голоса"])

    def test_large_consensus_embeds_respect_discord_field_limits(self) -> None:
        session = make_session()
        for user_id in range(4, 154):
            session.participants[user_id] = LiveParticipant(
                user_id,
                f"Участник {user_id}",
                f"<@{user_id}>",
                "senator",
                confirmed=True,
            )
        result = LiveResult(
            bill_id=10,
            bill_number=9,
            title="Большой состав",
            status="accepted",
            internal_percent=100.0,
            overall_percent=51.0,
            internal_active=True,
            votes={user_id: "yes" for user_id in session.participants},
        )
        session.results = [
            LiveResult(
                bill_id=index,
                bill_number=index,
                title="Очень длинное название проекта " * 5,
                status="accepted",
                internal_percent=100.0,
                overall_percent=51.0,
                internal_active=True,
                votes={},
            )
            for index in range(1, 80)
        ]

        for embed in (build_result_embed(result, session), build_final_summary_embed(session)):
            payload = embed.to_dict()
            self.assertTrue(all(len(field["name"]) <= 256 for field in payload.get("fields", [])))
            self.assertTrue(all(len(field["value"]) <= 1024 for field in payload.get("fields", [])))
            self.assertLessEqual(len(str(payload.get("description") or "")), 4096)

    def test_retry_bill_delivery_is_semantic_and_deterministic(self) -> None:
        session = make_session()
        result = LiveResult(
            bill_id=10,
            bill_number=9,
            title="Проект с вето",
            status="vetoed",
            internal_percent=0.0,
            overall_percent=0.0,
            internal_active=False,
            votes={1: "yes"},
            veto_by_id=1,
            retry_bill_number=10,
        )
        retry = {
            "id": 11,
            "guild_id": 77,
            "bill_number": 10,
            "author_id": 1,
            "title": "Повторное рассмотрение",
            "summary": "Описание",
            "status": "requeued",
            "attempt": 2,
        }

        first = build_retry_bill_delivery(session, result, retry)
        second = build_retry_bill_delivery(session, result, dict(retry))

        self.assertEqual(first, second)
        self.assertEqual(first["topic"], TVRS_RETRY_BILL_TOPIC)
        self.assertIn(":retry-bill:11", first["dedupe_key"])
        self.assertNotIn("embed", first["payload"])

    def test_control_and_terminal_deliveries_are_deterministic_semantic_jobs(self) -> None:
        session = make_session()
        session.participants[2].confirmed = False
        registration = build_control_dm_deliveries(session, phase="registration")
        self.assertEqual(len(registration), 1)
        self.assertEqual(registration[0]["topic"], TVRS_CONTROL_DM_TOPIC)
        self.assertEqual(registration[0]["payload"]["user_id"], 2)

        session.participants[2].confirmed = True
        session.stage = "voting"
        session.current_bill = {"id": 10, "bill_number": 9, "title": "Надёжная доставка"}
        voting = build_control_dm_deliveries(session, phase="voting")
        self.assertEqual(len(voting), 2)
        self.assertTrue(all(item["payload"]["bill_id"] == 10 for item in voting))

        session.results = [
            LiveResult(
                bill_id=10,
                bill_number=9,
                title="Надёжная доставка",
                status="accepted",
                internal_percent=100.0,
                overall_percent=51.0,
                internal_active=True,
                votes={1: "yes", 2: "yes", 3: "yes"},
            )
        ]
        summaries = build_session_summary_deliveries(session)
        self.assertEqual(len(summaries), 3)
        self.assertTrue(all(item["topic"] == TVRS_SESSION_SUMMARY_TOPIC for item in summaries))
        self.assertEqual(summaries, build_session_summary_deliveries(session))
        json.dumps([registration, voting, summaries], ensure_ascii=False)


class MarketDomainContractTests(unittest.TestCase):
    def test_ranker_is_storage_independent_and_supports_russian_transliteration(self) -> None:
        vehicle_id = market_internal_id("vehicles", "faggio")
        items = [
            {
                "item_id": vehicle_id,
                "external_id": "faggio",
                "category": "vehicles",
                "item_name": "Pegassi Faggio Sport",
                "normalized_name": "pegassi faggio sport",
                "sold_count": 9,
            },
            {
                "item_id": market_internal_id("vehicles", "sultan"),
                "external_id": "sultan",
                "category": "vehicles",
                "item_name": "Karin Sultan",
                "normalized_name": "karin sultan",
                "sold_count": 20,
            },
        ]

        hits = rank_market_items(items, "фаджио")

        self.assertEqual([hit.item["item_id"] for hit in hits], [vehicle_id])


class ConsensusRuntimeContractTests(unittest.TestCase):
    def tearDown(self) -> None:
        registry.sessions.clear()

    def test_runtime_snapshot_is_stable_and_does_not_expose_mutable_bill(self) -> None:
        session = make_session()
        session.stage = "voting"
        session.current_bill = {"id": 10, "bill_number": 9, "title": "Проект", "summary": "Скрыто"}
        registry.add(session)

        snapshot = active_consensus_snapshot(77)

        self.assertIs(active_sessions[77], session)
        self.assertEqual(snapshot["stage_label"], "голосование")  # type: ignore[index]
        self.assertEqual(
            snapshot["current_bill"],  # type: ignore[index]
            {"id": 10, "bill_number": 9, "title": "Проект"},
        )
        self.assertNotIn("summary", snapshot["current_bill"])  # type: ignore[operator,index]


class TVRSDurableDeliveryContractTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self) -> None:
        registry.sessions.clear()

    @staticmethod
    def message(job: dict, *, attempts: int = 1) -> OutboxMessage:
        return OutboxMessage(
            id=1,
            topic=str(job["topic"]),
            dedupe_key=str(job["dedupe_key"]),
            payload=dict(job["payload"]),
            attempts=attempts,
            max_attempts=12,
            lease_token="test-lease",
        )

    async def test_manual_dead_letter_replay_reuses_existing_marker_on_first_attempt(self) -> None:
        current = make_session()
        result = LiveResult(
            bill_id=10,
            bill_number=9,
            title="Однократная доставка",
            status="accepted",
            internal_percent=100.0,
            overall_percent=51.0,
            internal_active=True,
            votes={1: "yes", 2: "yes", 3: "yes"},
        )
        job = build_result_deliveries(current, result, participant_content="Готово")[0]
        channel = SimpleNamespace(send=AsyncMock())
        guild = SimpleNamespace(get_channel=lambda channel_id: channel)
        bot = SimpleNamespace(get_guild=lambda guild_id: guild, get_channel=lambda channel_id: None)
        previous = SimpleNamespace(id=7001)

        with (
            patch("modules.tvrs_delivery.storage.tvrs_live_result_for_bill", return_value={"id": 1}),
            patch(
                "modules.tvrs_delivery.find_delivery_marker",
                new=AsyncMock(return_value=previous),
            ) as marker_lookup,
        ):
            receipt = await make_result_delivery_handler(bot)(self.message(job, attempts=1))

        self.assertEqual(receipt.message_id, 7001)
        marker_lookup.assert_awaited_once()
        channel.send.assert_not_awaited()

    async def test_delayed_result_never_overwrites_control_message_for_next_bill(self) -> None:
        current = make_session()
        current.participants[2].vote_message_id = 9002
        result = LiveResult(
            bill_id=10,
            bill_number=9,
            title="Предыдущий проект",
            status="accepted",
            internal_percent=100.0,
            overall_percent=51.0,
            internal_active=True,
            votes={1: "yes", 2: "yes", 3: "yes"},
        )
        jobs = build_result_deliveries(current, result, participant_content="Готово")
        job = next(item for item in jobs if item["payload"].get("destination_user_id") == 2)

        current.stage = "voting"
        current.current_bill = {"id": 11, "bill_number": 10, "title": "Следующий проект"}
        registry.add(current)
        dm_channel = SimpleNamespace(fetch_message=AsyncMock())
        member = SimpleNamespace(
            dm_channel=dm_channel,
            create_dm=AsyncMock(return_value=dm_channel),
            send=AsyncMock(return_value=SimpleNamespace(id=9010)),
        )
        guild = SimpleNamespace(
            get_member=lambda user_id: member if user_id == 2 else None,
            fetch_member=AsyncMock(return_value=member),
        )
        bot = SimpleNamespace(get_guild=lambda guild_id: guild)

        with patch(
            "modules.tvrs_delivery.storage.tvrs_live_result_for_bill",
            return_value={"id": 1},
        ):
            receipt = await make_result_delivery_handler(bot)(self.message(job))

        self.assertEqual(receipt.message_id, 9010)
        dm_channel.fetch_message.assert_not_awaited()
        member.send.assert_awaited_once()

    async def test_marker_lookup_transport_failure_retries_without_duplicate_send(self) -> None:
        current = make_session()
        result = LiveResult(
            bill_id=10,
            bill_number=9,
            title="Повтор без дубля",
            status="accepted",
            internal_percent=100.0,
            overall_percent=51.0,
            internal_active=True,
            votes={1: "yes", 2: "yes", 3: "yes"},
        )
        job = build_result_deliveries(current, result, participant_content="Готово")[0]

        class FailingHistoryChannel:
            def __init__(self) -> None:
                self.send = AsyncMock()

            def history(self, *, limit: int):
                async def rows():
                    raise discord.DiscordException("temporary history failure")
                    yield None

                return rows()

        channel = FailingHistoryChannel()
        guild = SimpleNamespace(get_channel=lambda channel_id: channel)
        bot = SimpleNamespace(get_guild=lambda guild_id: guild, get_channel=lambda channel_id: None)

        with (
            patch("modules.tvrs_delivery.storage.tvrs_live_result_for_bill", return_value={"id": 1}),
            self.assertRaises(discord.DiscordException),
        ):
            await make_result_delivery_handler(bot)(self.message(job, attempts=2))

        channel.send.assert_not_awaited()

    async def test_dead_letter_replay_does_not_resurrect_deleted_bill(self) -> None:
        job = {
            "topic": TVRS_RETRY_BILL_TOPIC,
            "dedupe_key": "consensus:deleted:retry-bill:99",
            "payload": {
                "payload_version": 1,
                "guild_id": 77,
                "channel_id": 100,
                "bill": {"id": 99, "bill_number": 9, "title": "Удалённый проект"},
            },
        }
        bot = SimpleNamespace(get_guild=MagicMock(side_effect=AssertionError("Discord must not be called")))

        with patch("modules.tvrs_delivery.storage.tvrs_get_bill_dict_by_id", return_value=None):
            receipt = await make_retry_bill_delivery_handler(bot)(self.message(job, attempts=1))

        self.assertIsNone(receipt.message_id)
        bot.get_guild.assert_not_called()


class RuntimePortContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_hub_dispatches_to_registered_feature_without_importing_its_view(self) -> None:
        calls: list[tuple[int, str]] = []

        async def handler(interaction: object, requester_id: int, surface: str) -> None:
            self.assertIs(interaction, marker)
            calls.append((requester_id, surface))

        marker = SimpleNamespace()
        register_hub_section("__architecture_test__", handler)  # type: ignore[arg-type]

        await open_hub_section("__architecture_test__", marker, 77, surface="ephemeral")

        self.assertEqual(calls, [(77, "ephemeral")])

    async def test_operations_wakeup_port_is_shared_without_dashboard_import(self) -> None:
        event = bind_worker_wakeup(asyncio.Event())

        wake_operations_worker()

        self.assertTrue(event.is_set())


if __name__ == "__main__":
    unittest.main()
