import tempfile
import unittest
from pathlib import Path

import storage

from modules.bill_editor_ai import (
    build_bill_editor_prompt,
    parse_bill_editor_draft,
)
from modules.consensus_core import (
    ConsensusRules,
    LiveConsensusSession,
    LiveParticipant,
    LiveResult,
    calculate_consensus,
)
from modules.tvrs_delivery import build_session_summary_deliveries
from modules.tvrs_embeds import build_final_summary_embed


def participant(
    user_id: int,
    *,
    block: str | None = None,
) -> LiveParticipant:
    return LiveParticipant(
        user_id=user_id,
        display_name=f"Участник {user_id}",
        mention=f"<@{user_id}>",
        kind="chair" if block else "senator",
        confirmed=True,
        voting_block=block,  # type: ignore[arg-type]
    )


def act_iii_session(category: str = "ordinary") -> LiveConsensusSession:
    participants = {
        1: participant(1, block="first"),
        2: participant(2, block="second"),
        3: participant(3, block="third"),
        4: participant(4),
        5: participant(5),
        6: participant(6),
    }
    return LiveConsensusSession(
        session_key="act-iii",
        guild_id=77,
        channel_id=88,
        leader_id=1,
        leader_display="Первый",
        plenary_number=5,
        participants=participants,
        current_bill={
            "id": 10,
            "bill_number": 10,
            "title": "О новой системе",
            "decision_category": category,
        },
        stage="voting",
        rules=ConsensusRules(),
    )


class ConsensusActIIITests(unittest.TestCase):
    def test_four_equal_blocks_accept_an_ordinary_decision_with_two_blocks(self) -> None:
        session = act_iii_session()
        session.votes = {
            1: "yes",
            2: "no",
            3: "yes",
            4: "yes",
            5: "yes",
            6: "no",
        }

        result = calculate_consensus(session)

        self.assertEqual(result["internal_percent"], 66.67)
        self.assertEqual(result["overall_percent"], 75.0)
        self.assertEqual(result["opposed_percent"], 25.0)
        self.assertEqual(
            result["block_votes"],
            {
                "first": "yes",
                "second": "no",
                "third": "yes",
                "consensus": "yes",
            },
        )
        self.assertTrue(result["accepted"])

    def test_exact_two_by_two_split_is_rejected(self) -> None:
        session = act_iii_session()
        session.votes = {
            1: "yes",
            2: "yes",
            3: "no",
            4: "no",
            5: "no",
            6: "no",
        }

        result = calculate_consensus(session)

        self.assertEqual(result["overall_percent"], 50.0)
        self.assertEqual(result["opposed_percent"], 50.0)
        self.assertFalse(result["accepted"])

    def test_abstentions_count_for_roster_but_not_internal_yes_no(self) -> None:
        session = act_iii_session()
        session.votes = {
            1: "yes",
            2: "abstain",
            3: "yes",
            4: "yes",
            5: "no",
            6: "abstain",
        }

        result = calculate_consensus(session)

        self.assertEqual(result["internal_valid_votes"], 4)
        self.assertEqual(result["internal_abstentions"], 2)
        self.assertEqual(result["internal_percent"], 75.0)
        self.assertEqual(result["block_votes"]["second"], "abstain")

    def test_heavy_decision_needs_three_blocks(self) -> None:
        session = act_iii_session("heavy")
        session.votes = {
            1: "yes",
            2: "no",
            3: "yes",
            4: "yes",
            5: "yes",
            6: "no",
        }

        self.assertTrue(calculate_consensus(session)["accepted"])
        session.votes[4] = "no"
        session.votes[5] = "no"
        self.assertFalse(calculate_consensus(session)["accepted"])

    def test_administrative_leader_never_fills_an_absent_cochair_block(self) -> None:
        session = act_iii_session()
        del session.participants[1]
        session.leader_id = 7
        session.participants[7] = LiveParticipant(
            user_id=7,
            display_name="Администратор",
            mention="<@7>",
            kind="chair",
            confirmed=True,
            voting_block=None,
        )
        session.votes = {
            2: "no",
            3: "no",
            4: "yes",
            5: "yes",
            6: "yes",
            7: "yes",
        }

        result = calculate_consensus(session)

        self.assertEqual(result["block_votes"]["first"], "inactive")
        self.assertEqual(result["overall_percent"], 25.0)


class ConsensusSummaryTests(unittest.TestCase):
    def test_result_titles_link_to_original_submission(self) -> None:
        session = act_iii_session()
        session.stage = "finished"
        session.finished = True
        session.results = [
            LiveResult(
                bill_id=10,
                bill_number=10,
                title="О новой системе",
                status="accepted",
                internal_percent=66.67,
                overall_percent=75.0,
                internal_active=True,
                votes={},
                source_channel_id=123,
                source_message_id=456,
                required_percent=50.0,
            )
        ]

        embed = build_final_summary_embed(session)
        rendered = "\n".join(str(field.value) for field in embed.fields)

        self.assertIn(
            "[О новой системе](https://discord.com/channels/77/123/456)",
            rendered,
        )

    def test_large_summary_is_split_into_durable_public_pages(self) -> None:
        session = act_iii_session()
        session.stage = "finished"
        session.finished = True
        session.results = [
            LiveResult(
                bill_id=index,
                bill_number=index,
                title=f"Очень подробный законопроект {index} " + ("я" * 130),
                status="accepted" if index % 2 else "rejected",
                internal_percent=60.0,
                overall_percent=75.0,
                internal_active=True,
                votes={},
                source_channel_id=123,
                source_message_id=1000 + index,
                required_percent=50.0,
            )
            for index in range(1, 61)
        ]

        deliveries = build_session_summary_deliveries(session)
        public = [
            item
            for item in deliveries
            if item["payload"]["destination"] == "public"
        ]

        self.assertGreater(len(public), 1)
        self.assertEqual(
            sum(len(item["payload"]["results"]) for item in public),
            60,
        )
        self.assertEqual(
            {item["payload"]["summary_counts"]["total"] for item in public},
            {60},
        )
        self.assertEqual(len({item["dedupe_key"] for item in public}), len(public))


class BillEditorAiTests(unittest.TestCase):
    def test_json_fences_are_removed_and_payload_is_validated(self) -> None:
        draft = parse_bill_editor_draft(
            """```json
            {
              "title": "О едином справочнике участников",
              "summary": "Предлагается вести единые карточки участников в профиле T-Mod.",
              "materials": "",
              "decision_category": "ordinary",
              "implementation_plan": "Добавить форму и открыть просмотр через команду profile.",
              "leadership_actions": "Сообщить новым участникам и контролировать заполнение.",
              "clarification": "Уточнить ответственного за контроль."
            }
            ```"""
        )

        self.assertEqual(draft.decision_category, "ordinary")
        self.assertIsNone(draft.materials)
        self.assertIn("справочнике", draft.title)

    def test_prompt_contains_act_iii_boundaries_and_author_facts(self) -> None:
        prompt = build_bill_editor_prompt(
            idea="Создать справочник",
            desired_outcome="Участники знают друг друга",
            constraints_text="Без отдельного канала",
        )

        self.assertIn("четыре равных блока по 25%", prompt)
        self.assertIn("Не добавляй фактов", prompt)
        self.assertIn("Без отдельного канала", prompt)


class BillWorkspaceStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_data_dir = storage.DATA_DIR
        self.old_database_file = storage.DATABASE_FILE
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(self.temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "bill-editor-test.db"
        storage.init_db()

    def tearDown(self) -> None:
        storage.DATA_DIR = self.old_data_dir
        storage.DATABASE_FILE = self.old_database_file
        self.temp_dir.cleanup()

    def test_workspace_is_single_revisioned_and_submission_is_idempotent(self) -> None:
        workspace, created = storage.create_or_get_bill_workspace(
            guild_id=77,
            author_id=5,
            author_display="Автор",
            parent_channel_id=88,
        )
        same, same_created = storage.create_or_get_bill_workspace(
            guild_id=77,
            author_id=5,
            author_display="Автор",
            parent_channel_id=88,
        )
        self.assertTrue(created)
        self.assertFalse(same_created)
        self.assertEqual(workspace["id"], same["id"])

        updated = storage.update_bill_workspace(
            workspace["id"],
            expected_revision=workspace["revision"],
            idea="Нужен единый справочник участников",
            desired_outcome="Упростить знакомство",
            title="О справочнике участников",
            summary="Предлагается добавить единый справочник в профиль каждого участника.",
            implementation_plan="Добавить поля профиля и процедуру заполнения.",
            leadership_actions="Проверять заполнение при приёме новых участников.",
            status="review",
        )
        with self.assertRaisesRegex(ValueError, "bill_workspace_revision_conflict"):
            storage.update_bill_workspace(
                workspace["id"],
                expected_revision=workspace["revision"],
                title="Устаревшая правка",
            )

        values = dict(
            guild_id=77,
            channel_id=88,
            author_id=5,
            author_display="Автор",
            title=updated["title"],
            summary=updated["summary"],
            materials=None,
            decision_category="ordinary",
            implementation_plan=updated["implementation_plan"],
            leadership_actions=updated["leadership_actions"],
            editor_workspace_id=updated["id"],
            delivery_topic="test.bill",
        )
        first, _, first_created = storage.tvrs_create_bill_with_publication(**values)
        second, _, second_created = storage.tvrs_create_bill_with_publication(**values)

        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first.id, second.id)
        closed = storage.finish_bill_workspace(
            updated["id"],
            author_id=5,
            status="submitted",
            submitted_bill_id=first.id,
        )
        self.assertEqual(closed["status"], "submitted")
        self.assertEqual(closed["submitted_bill_id"], first.id)


class MemberDirectoryStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_data_dir = storage.DATA_DIR
        self.old_database_file = storage.DATABASE_FILE
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(self.temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "member-directory-test.db"
        storage.init_db()

    def tearDown(self) -> None:
        storage.DATA_DIR = self.old_data_dir
        storage.DATABASE_FILE = self.old_database_file
        self.temp_dir.cleanup()

    def test_new_admission_requirement_is_cleared_by_completed_card(self) -> None:
        profile, newly_required = storage.require_member_directory(
            77,
            5,
            prompted=True,
        )
        self.assertTrue(newly_required)
        self.assertTrue(profile.directory_required)
        self.assertIsNotNone(profile.onboarding_prompted_at)

        completed = storage.update_member_directory(
            77,
            5,
            biography="Участник Товарищества",
            contribution="Работаю с инфраструктурой",
            responsibilities="Интересуюсь автоматизацией и документацией",
            membership_since="2026-07-25",
        )

        self.assertFalse(completed.directory_required)
        self.assertIsNotNone(completed.directory_completed_at)
        self.assertEqual(completed.membership_since, "2026-07-25")


if __name__ == "__main__":
    unittest.main()
