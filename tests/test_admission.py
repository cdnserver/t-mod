import tempfile
import unittest
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import storage
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from modules.admission import (
    ADMISSION_QUESTIONS,
    AdmissionPublicView,
    ensure_membership_bill,
    evaluate_answers,
    notify_ovr_desks,
    reconcile_admission_pipeline,
)
from modules.admission_web import register_admission_web_routes
from persistence import admission_repository as admission_storage
from persistence import reactor_repository as reactor_storage
from persistence import web_auth_repository as web_auth_storage


class AdmissionPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_data_dir = storage.DATA_DIR
        self.old_database_file = storage.DATABASE_FILE
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(self.temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "admission-test.db"
        storage.init_db()

    def tearDown(self) -> None:
        storage.DATA_DIR = self.old_data_dir
        storage.DATABASE_FILE = self.old_database_file
        self.temp_dir.cleanup()

    @staticmethod
    def answers() -> dict[str, str]:
        return {
            str(question["id"]): str(question["options"][0][0])
            for question in ADMISSION_QUESTIONS
        }

    def submit(self, *, user_id: int = 101) -> dict:
        answers, traits = evaluate_answers(self.answers())
        return admission_storage.submit_application(
            guild_id=77,
            user_id=user_id,
            user_display=f"Кандидат {user_id}",
            forum_url="https://forum.majestic-rp.ru/members/candidate.101/",
            characters=[
                {"nickname": "Saul Goodman", "static_id": "263345"},
                {"nickname": "Jimmy McGill", "static_id": "263346"},
            ],
            answers=answers,
            traits=traits,
            motivation="Хочу системно участвовать в работе и отвечать за результат.",
            contribution="Готов вести проекты и помогать другим участникам.",
            availability="По будням после 18:00.",
        )

    def test_submission_atomically_creates_ovr_case_tasks_events_and_delivery(
        self,
    ) -> None:
        detail = self.submit()
        application = detail["application"]

        self.assertEqual(application["status"], "ovr_review")
        self.assertEqual(application["ovr_case_number"], 1)
        self.assertEqual(len(application["characters"]), 2)
        self.assertEqual(len(detail["events"]), 1)

        with storage.connect_readonly() as con:
            ovr_case = con.execute(
                "SELECT * FROM ovr_cases WHERE id = ?",
                (application["ovr_case_id"],),
            ).fetchone()
            task_count = con.execute(
                "SELECT COUNT(*) AS n FROM ovr_case_tasks WHERE case_id = ?",
                (application["ovr_case_id"],),
            ).fetchone()["n"]
            deliveries = con.execute(
                "SELECT payload_json, status FROM delivery_outbox ORDER BY id"
            ).fetchall()

        self.assertEqual(ovr_case["case_kind"], "admission")
        self.assertEqual(task_count, 3)
        self.assertEqual(len(deliveries), 2)
        self.assertTrue(all(row["status"] == "pending" for row in deliveries))

        with self.assertRaisesRegex(ValueError, "admission_application_already_exists"):
            self.submit()

    def test_full_decision_chain_is_idempotent_and_preserves_references(self) -> None:
        application = self.submit()["application"]
        approved, changed = admission_storage.record_ovr_decision(
            guild_id=77,
            case_id=application["ovr_case_id"],
            approved=True,
            actor_id=501,
            actor_display="Сотрудник ОВР",
            note="Риски не установлены, кандидат допускается.",
        )

        self.assertTrue(changed)
        self.assertEqual(approved["status"], "ovr_approved")
        self.assertEqual(approved["ovr_case_number"], 1)
        repeated, changed_again = admission_storage.record_ovr_decision(
            guild_id=77,
            case_id=application["ovr_case_id"],
            approved=True,
            actor_id=501,
            actor_display="Сотрудник ОВР",
            note="Повторная доставка того же решения.",
        )
        self.assertFalse(changed_again)
        self.assertEqual(repeated["status"], "ovr_approved")

        queued, linked = admission_storage.link_consensus_bill(
            application["id"],
            guild_id=77,
            bill_id=901,
            bill_number=74,
            actor_id=999,
            actor_display="T-Mod · Phoenix",
        )
        self.assertTrue(linked)
        self.assertEqual(queued["status"], "consensus_queued")
        self.assertEqual(queued["submitted_bill_number"], 74)

        final, recorded = admission_storage.record_consensus_result(
            application["id"],
            guild_id=77,
            result_status="accepted",
            actor_id=999,
            actor_display="T-Mod · Consensus",
        )
        self.assertTrue(recorded)
        self.assertEqual(final["status"], "membership_approved")
        self.assertEqual(final["consensus_result"], "accepted")

        persisted = admission_storage.application_detail(77, 101)
        self.assertIsNotNone(persisted)
        self.assertEqual(len(persisted["events"]), 4)
        with storage.connect_readonly() as con:
            outbox_count = con.execute(
                "SELECT COUNT(*) AS n FROM delivery_outbox"
            ).fetchone()["n"]
        self.assertEqual(outbox_count, 8)

    def test_crash_window_between_ovr_and_phoenix_is_detected(self) -> None:
        application = self.submit(user_id=202)["application"]
        with storage.connect() as con:
            con.execute(
                """
                UPDATE ovr_cases
                SET status = 'denied', decision = 'denied',
                    decision_reason = 'Недостаточно подтверждённых сведений.'
                WHERE id = ?
                """,
                (application["ovr_case_id"],),
            )
            con.commit()

        stranded = admission_storage.unreconciled_ovr_decisions(77)

        self.assertEqual(len(stranded), 1)
        self.assertEqual(stranded[0]["status"], "denied")
        recovered, changed = admission_storage.record_ovr_decision(
            guild_id=77,
            case_id=application["ovr_case_id"],
            approved=False,
            actor_id=999,
            actor_display="T-Mod · Recovery",
            note=stranded[0]["decision_reason"],
        )
        self.assertTrue(changed)
        self.assertEqual(recovered["status"], "ovr_denied")
        self.assertEqual(admission_storage.unreconciled_ovr_decisions(77), [])

    def test_real_bill_and_consensus_result_complete_the_pipeline(self) -> None:
        application = self.submit(user_id=303)["application"]
        approved, _ = admission_storage.record_ovr_decision(
            guild_id=77,
            case_id=application["ovr_case_id"],
            approved=True,
            actor_id=501,
            actor_display="Сотрудник ОВР",
            note="Кандидат допущен к рассмотрению.",
        )
        bot = SimpleNamespace(user=SimpleNamespace(id=999))
        guild = SimpleNamespace(id=77)

        self.assertTrue(asyncio.run(ensure_membership_bill(bot, guild, approved)))
        queued = admission_storage.application_detail(77, 303)["application"]
        bill = storage.tvrs_get_bill_dict_by_id(queued["submitted_bill_id"])
        self.assertIn("ОВР-001", bill["materials"])
        self.assertEqual(bill["decision_category"], "ordinary")

        storage.tvrs_save_live_result(
            guild_id=77,
            session_key="77:admission-consensus",
            plenary_number=8,
            bill_id=bill["id"],
            bill_number=bill["bill_number"],
            bill_title=bill["title"],
            status="accepted",
            internal_percent=100.0,
            overall_percent=100.0,
            internal_active=True,
            votes_json="{}",
        )
        changed = asyncio.run(reconcile_admission_pipeline(bot, guild))

        self.assertEqual(changed, 1)
        final = admission_storage.application_detail(77, 303)["application"]
        self.assertEqual(final["status"], "membership_approved")
        self.assertEqual(final["consensus_result"], "accepted")

    def test_new_application_notifies_ovr_grants_and_administrators_once(self) -> None:
        application = self.submit(user_id=304)["application"]
        web_auth_storage.web_set_section_grant(
            77,
            700,
            "ovr",
            enabled=True,
            granted_by_id=999,
        )
        guild = SimpleNamespace(
            id=77,
            members=[
                SimpleNamespace(
                    id=701,
                    guild_permissions=SimpleNamespace(administrator=True),
                ),
                SimpleNamespace(
                    id=702,
                    guild_permissions=SimpleNamespace(administrator=False),
                ),
            ],
        )

        first = asyncio.run(notify_ovr_desks(guild, application))
        second = asyncio.run(notify_ovr_desks(guild, application))

        self.assertEqual(first, 2)
        self.assertEqual(second, 2)
        self.assertEqual(
            reactor_storage.reactor_list_notifications(77, 700)["unread"], 1
        )
        self.assertEqual(
            reactor_storage.reactor_list_notifications(77, 701)["unread"], 1
        )
        self.assertEqual(
            reactor_storage.reactor_list_notifications(77, 702)["unread"], 0
        )


class AdmissionQuestionTests(unittest.TestCase):
    def test_every_question_is_required_and_scores_are_bounded(self) -> None:
        incomplete = {
            str(question["id"]): str(question["options"][0][0])
            for question in ADMISSION_QUESTIONS[:-1]
        }
        with self.assertRaisesRegex(ValueError, "admission_answers_incomplete"):
            evaluate_answers(incomplete)

        complete = {
            str(question["id"]): str(question["options"][0][0])
            for question in ADMISSION_QUESTIONS
        }
        clean, traits = evaluate_answers(complete)
        self.assertEqual(len(clean), len(ADMISSION_QUESTIONS))
        self.assertTrue(traits)
        self.assertTrue(all(15 <= value <= 95 for value in traits.values()))

    def test_public_panel_has_clear_application_account_and_discord_actions(self) -> None:
        view = AdmissionPublicView(1500495112638038246)
        labels = [getattr(item, "label", None) for item in view.children]
        self.assertEqual(
            labels,
            ["Подать заявку", "Создать аккаунт", "Вступить в Discord"],
        )


class AdmissionWebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.old_data_dir = storage.DATA_DIR
        self.old_database_file = storage.DATABASE_FILE
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(self.temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "admission-web-test.db"
        storage.init_db()
        app = web.Application()
        register_admission_web_routes(
            app,
            SimpleNamespace(),
            guild_id=77,
            asset_dir=Path(__file__).resolve().parents[1] / "web" / "consensus",
        )
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        storage.DATA_DIR = self.old_data_dir
        storage.DATABASE_FILE = self.old_database_file
        self.temp_dir.cleanup()

    async def test_public_bootstrap_and_authenticated_idempotent_submission(
        self,
    ) -> None:
        anonymous = await self.client.get("/api/admission")
        self.assertEqual(anonymous.status, 200)
        self.assertFalse((await anonymous.json())["authenticated"])

        banner = await self.client.get(
            "/admission-assets/phoenix-senate-banner.webp"
        )
        self.assertEqual(banner.status, 200)
        self.assertEqual(banner.content_type, "image/webp")

        principal = SimpleNamespace(
            user_id=404,
            display_name="Кандидат 404",
            account_tier="zero",
            csrf_token="csrf-admission",
            member=SimpleNamespace(roles=[]),
        )
        characters = [
            SimpleNamespace(
                id=1, nickname="Saul Goodman", static_id="263345", position=1
            )
        ]
        answers = {
            str(question["id"]): str(question["options"][0][0])
            for question in ADMISSION_QUESTIONS
        }
        payload = {
            "forum_url": "https://forum.majestic-rp.ru/members/candidate.404/",
            "characters": [{"nickname": "Saul Goodman", "static_id": "263345"}],
            "motivation": "Хочу участвовать в общей работе и отвечать за результат.",
            "contribution": "Готов вести задачи и помогать другим.",
            "availability": "Вечером по будням.",
            "answers": answers,
        }
        with (
            patch(
                "modules.admission_web.resolve_principal",
                new=AsyncMock(return_value=principal),
            ),
            patch(
                "modules.admission_web.profile_storage.list_profile_characters",
                return_value=characters,
            ),
        ):
            first = await self.client.post(
                "/api/admission",
                json=payload,
                headers={
                    "X-CSRF-Token": "csrf-admission",
                    "X-Idempotency-Key": "same-submission-404",
                },
            )
            second = await self.client.post(
                "/api/admission",
                json=payload,
                headers={
                    "X-CSRF-Token": "csrf-admission",
                    "X-Idempotency-Key": "same-submission-404",
                },
            )

        self.assertEqual(first.status, 201)
        self.assertEqual(second.status, 200)
        self.assertEqual(
            (await first.json())["application"]["id"],
            (await second.json())["application"]["id"],
        )
        with storage.connect_readonly() as con:
            count = con.execute(
                "SELECT COUNT(*) AS n FROM membership_applications"
            ).fetchone()["n"]
        self.assertEqual(count, 1)

    async def test_zero_account_is_authenticated_before_character_setup(self) -> None:
        principal = SimpleNamespace(
            user_id=405,
            display_name="Новый пользователь",
            account_tier="zero",
            csrf_token="csrf-zero",
            member=SimpleNamespace(roles=[]),
        )
        with (
            patch(
                "modules.admission_web.resolve_principal",
                new=AsyncMock(return_value=principal),
            ),
            patch(
                "modules.admission_web.profile_storage.list_profile_characters",
                return_value=[],
            ),
        ):
            response = await self.client.get("/api/admission")
            payload = await response.json()

        self.assertEqual(response.status, 200)
        self.assertTrue(payload["authenticated"])
        self.assertTrue(payload["account_required"])
        self.assertEqual(payload["viewer"]["tier"], "zero")
        self.assertEqual(payload["viewer"]["name"], "Новый пользователь")


if __name__ == "__main__":
    unittest.main()
