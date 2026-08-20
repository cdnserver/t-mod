import gc
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
from aiohttp.test_utils import TestClient, TestServer

from modules.consensus_web import create_consensus_web_app
from modules.consensus_web_auth import ConsensusWebPrincipal
from modules import sgcontract
from persistence import core, schema, sgl_repository


class _ContractDeliveryFailure(discord.HTTPException):
    """Small HTTPException double for Discord delivery failures."""

    def __init__(self) -> None:
        pass


class _FakeTextChannel:
    def __init__(self, *, fail_on_send: int | None = None) -> None:
        self.fail_on_send = fail_on_send
        self.calls: list[dict[str, object]] = []

    async def send(self, **kwargs: object) -> SimpleNamespace:
        self.calls.append(kwargs)
        for file in list(kwargs.get("files") or []):
            file.close()  # type: ignore[union-attr]
        if self.fail_on_send is not None and len(self.calls) == self.fail_on_send:
            raise _ContractDeliveryFailure()
        return SimpleNamespace(id=9000 + len(self.calls))


class _FakeGuild:
    def get_member(self, _user_id: int) -> None:
        return None


class SGLContractWebTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.previous_data_dir = core.DATA_DIR
        self.previous_database_file = core.DATABASE_FILE
        core.DATA_DIR = Path(self.temp_dir.name)
        core.DATABASE_FILE = core.DATA_DIR / "sgl-contract-web.db"
        schema.init_db()
        reserved = sgl_repository.reserve_sgl_case(
            guild_id=77,
            client_id=101,
            client_display="Client",
            lead_lawyer_id=202,
            lead_lawyer_display="Lawyer",
            secretary_id=None,
            secretary_display=None,
            created_by_id=202,
            created_by_display="Lawyer",
        )
        self.case = sgl_repository.attach_sgl_case_channel(reserved.id, 404)
        assert self.case is not None
        self.page = Path(self.temp_dir.name) / "page-1.jpg"
        self.page.write_bytes(b"contract-page")

    def tearDown(self) -> None:
        core.DATA_DIR = self.previous_data_dir
        core.DATABASE_FILE = self.previous_database_file
        gc.collect()
        self.temp_dir.cleanup()

    @staticmethod
    def _manager() -> ConsensusWebPrincipal:
        member = SimpleNamespace(
            id=202,
            display_name="Lawyer",
            guild_permissions=SimpleNamespace(administrator=True),
            roles=[],
        )
        return ConsensusWebPrincipal(
            user_id=202,
            guild_id=77,
            display_name="Lawyer",
            csrf_token="csrf-contract-token",
            member=member,  # type: ignore[arg-type]
        )

    @staticmethod
    def _participant() -> ConsensusWebPrincipal:
        member = SimpleNamespace(
            id=101,
            display_name="Client",
            guild_permissions=SimpleNamespace(administrator=False),
            roles=[],
        )
        return ConsensusWebPrincipal(
            user_id=101,
            guild_id=77,
            display_name="Client",
            csrf_token="csrf-client-token",
            member=member,  # type: ignore[arg-type]
        )

    def _bot(self, channel: _FakeTextChannel | None = None) -> SimpleNamespace:
        guild = _FakeGuild()
        return SimpleNamespace(
            get_guild=lambda identifier: guild if identifier == 77 else None,
            get_channel=lambda identifier: channel if identifier == 404 else None,
        )

    @staticmethod
    def _generated(*pages: Path) -> dict[str, object]:
        return {
            "values": {
                "contractNumber": "SGL-001/alpha",
                "clientName": "Client",
                "lawyerName": "Lawyer",
                "lawyerStatic": "42",
            },
            "pages": list(pages),
        }

    async def test_defaults_and_preview_are_manager_only(self) -> None:
        app = create_consensus_web_app(self._bot(), guild_id=77)  # type: ignore[arg-type]
        async with TestClient(TestServer(app)) as client:
            with patch(
                "modules.consensus_web.resolve_principal",
                AsyncMock(return_value=self._manager()),
            ):
                manager_preview = await client.get(
                    f"/api/sgl/cases/{self.case.case_number}/contract",
                    headers={"Host": "sgl.tvr.lat"},
                )
                manager_payload = await manager_preview.json()
            with patch(
                "modules.consensus_web.resolve_principal",
                AsyncMock(return_value=self._participant()),
            ):
                participant_preview = await client.get(
                    f"/api/sgl/cases/{self.case.case_number}/contract",
                    headers={"Host": "sgl.tvr.lat"},
                )
                participant_detail = await client.get(
                    f"/api/sgl/cases/{self.case.case_number}",
                    headers={"Host": "sgl.tvr.lat"},
                )
                participant_preview_payload = await participant_preview.json()
                participant_detail_payload = await participant_detail.json()

        self.assertEqual(manager_preview.status, 200)
        self.assertEqual(manager_payload["defaults"]["contractNumber"], "SGL-001")
        self.assertEqual((participant_preview.status, participant_preview_payload["error"]), (403, "sgl_management_required"))
        self.assertEqual(participant_detail.status, 200)
        self.assertEqual(participant_detail_payload["contract"], {"available": False})

    async def test_post_sends_only_valid_generated_pages_and_sanitizes_attachment_name(self) -> None:
        channel = _FakeTextChannel()
        app = create_consensus_web_app(self._bot(channel), guild_id=77)  # type: ignore[arg-type]
        generated = Mock(return_value=self._generated(self.page))
        headers = {"Host": "sgl.tvr.lat", "X-CSRF-Token": "csrf-contract-token"}
        with patch("modules.sgl_web.discord.TextChannel", _FakeTextChannel), patch(
            "modules.sgl_web.generate_contract_files", generated
        ), patch(
            "modules.consensus_web.resolve_principal", AsyncMock(return_value=self._manager())
        ):
            async with TestClient(TestServer(app)) as client:
                response = await client.post(
                    f"/api/sgl/cases/{self.case.case_number}/contract",
                    headers=headers,
                    json={
                        "overrides": {
                            "contractNumber": "  SGL-001/alpha  ",
                            "clientName": "Updated client",
                            "notAllowed": "must not reach generator",
                        }
                    },
                )
                payload = await response.json()

        self.assertEqual(response.status, 201)
        self.assertEqual(payload["message_ids"], [9001])
        self.assertEqual(payload["pages"], 1)
        self.assertEqual(
            generated.call_args.args[2],
            {"contractNumber": "SGL-001/alpha", "clientName": "Updated client"},
        )
        self.assertEqual(len(channel.calls), 1)
        files = channel.calls[0]["files"]
        self.assertEqual(files[0].filename, "SGL-001-alpha_page_1.jpg")  # type: ignore[index,union-attr]

    async def test_post_never_reports_success_for_empty_or_failed_generation(self) -> None:
        channel = _FakeTextChannel()
        app = create_consensus_web_app(self._bot(channel), guild_id=77)  # type: ignore[arg-type]
        headers = {"Host": "sgl.tvr.lat", "X-CSRF-Token": "csrf-contract-token"}
        with patch("modules.sgl_web.discord.TextChannel", _FakeTextChannel), patch(
            "modules.consensus_web.resolve_principal", AsyncMock(return_value=self._manager())
        ):
            async with TestClient(TestServer(app)) as client:
                with patch(
                    "modules.sgl_web.generate_contract_files",
                    Mock(return_value=self._generated()),
                ):
                    empty_response = await client.post(
                        f"/api/sgl/cases/{self.case.case_number}/contract",
                        headers=headers,
                        json={"overrides": {}},
                    )
                    empty_payload = await empty_response.json()
                with patch(
                    "modules.sgl_web.generate_contract_files",
                    Mock(side_effect=RuntimeError("/internal/path/template failure")),
                ):
                    failed_response = await client.post(
                        f"/api/sgl/cases/{self.case.case_number}/contract",
                        headers=headers,
                        json={"overrides": {}},
                    )
                    failed_payload = await failed_response.json()

        self.assertEqual((empty_response.status, empty_payload["error"]), (502, "sgl_contract_generation_failed"))
        self.assertEqual((failed_response.status, failed_payload["error"]), (502, "sgl_contract_generation_failed"))
        self.assertNotIn("/internal/path", failed_payload["message"])
        self.assertEqual(channel.calls, [])

    async def test_post_distinguishes_delivery_failure_and_partial_delivery(self) -> None:
        first_failure = _FakeTextChannel(fail_on_send=1)
        app = create_consensus_web_app(self._bot(first_failure), guild_id=77)  # type: ignore[arg-type]
        headers = {"Host": "sgl.tvr.lat", "X-CSRF-Token": "csrf-contract-token"}
        with patch("modules.sgl_web.discord.TextChannel", _FakeTextChannel), patch(
            "modules.consensus_web.resolve_principal", AsyncMock(return_value=self._manager())
        ):
            async with TestClient(TestServer(app)) as client:
                with patch(
                    "modules.sgl_web.generate_contract_files",
                    Mock(return_value=self._generated(self.page)),
                ):
                    delivery_response = await client.post(
                        f"/api/sgl/cases/{self.case.case_number}/contract",
                        headers=headers,
                        json={"overrides": {}},
                    )
                    delivery_payload = await delivery_response.json()

        pages = []
        for number in range(11):
            page = Path(self.temp_dir.name) / f"page-{number + 1}.jpg"
            page.write_bytes(b"contract-page")
            pages.append(page)
        partial_channel = _FakeTextChannel(fail_on_send=2)
        partial_app = create_consensus_web_app(self._bot(partial_channel), guild_id=77)  # type: ignore[arg-type]
        with patch("modules.sgl_web.discord.TextChannel", _FakeTextChannel), patch(
            "modules.consensus_web.resolve_principal", AsyncMock(return_value=self._manager())
        ), patch(
            "modules.sgl_web.generate_contract_files", Mock(return_value=self._generated(*pages))
        ):
            async with TestClient(TestServer(partial_app)) as client:
                partial_response = await client.post(
                    f"/api/sgl/cases/{self.case.case_number}/contract",
                    headers=headers,
                    json={"overrides": {}},
                )
                partial_payload = await partial_response.json()

        self.assertEqual((delivery_response.status, delivery_payload["error"]), (502, "sgl_contract_delivery_failed"))
        self.assertEqual((partial_response.status, partial_payload["error"]), (502, "sgl_contract_delivery_partial"))
        self.assertEqual(partial_payload["message_ids"], [9001])
        self.assertEqual((partial_payload["pages_sent"], partial_payload["pages_total"]), (10, 11))


class SGLContractGenerationTests(unittest.TestCase):
    def test_generation_uses_a_distinct_directory_for_concurrent_same_second_runs(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            fixed_now = datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc)

            def fake_pdf(_docx: Path, output: Path) -> Path:
                pdf = output / "contract.pdf"
                pdf.write_bytes(b"pdf")
                return pdf

            def fake_pages(_pdf: Path, output: Path) -> list[Path]:
                page = output / "page-1.jpg"
                page.write_bytes(b"page")
                return [page]

            values = {key: "value" for key in sgcontract.PLACEHOLDERS}
            values["contractNumber"] = "SGL-001"
            with patch.object(sgcontract, "OUTPUT_DIR", root), patch.object(
                sgcontract, "default_values", return_value=values
            ), patch.object(sgcontract, "_ensure_template_exists", return_value=root / "template.docx"), patch.object(
                sgcontract, "_replace_docx_placeholders"
            ), patch.object(sgcontract, "_convert_docx_to_pdf", side_effect=fake_pdf), patch.object(
                sgcontract, "_convert_pdf_to_jpgs", side_effect=fake_pages
            ), patch.object(sgcontract, "datetime") as patched_datetime:
                patched_datetime.now.return_value = fixed_now
                first = sgcontract.generate_contract_files(SimpleNamespace(), None, {})
                second = sgcontract.generate_contract_files(SimpleNamespace(), None, {})

        self.assertNotEqual(first["dir"], second["dir"])
        self.assertTrue(str(first["dir"]).startswith(str(root)))
        self.assertTrue(str(second["dir"]).startswith(str(root)))


if __name__ == "__main__":
    unittest.main()
