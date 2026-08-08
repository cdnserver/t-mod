import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from modules import sgbureau


class FakeGuild:
    def __init__(self, *members):
        self.members = list(members)
        self._members = {member.id: member for member in members}

    def get_member(self, member_id):
        return self._members.get(int(member_id))


class SGLCaseCreationViewTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_reply_ignores_deleted_interaction_channel(self):
        unknown_channel = discord.HTTPException(
            SimpleNamespace(status=400, reason="Bad Request"),
            {"message": "Unknown Channel", "code": 10003},
        )
        interaction = SimpleNamespace(
            followup=SimpleNamespace(send=AsyncMock(side_effect=unknown_channel)),
        )

        delivered = await sgbureau._send_admin_modal_reply_safely(
            interaction,
            "Готово",
        )

        self.assertFalse(delivered)
        interaction.followup.send.assert_awaited_once_with("Готово", ephemeral=True)

    def test_case_creation_view_exposes_optional_secretary_selector(self):
        view = sgbureau.SGCaseCreationView(
            bot=None,
            requester_id=10,
            client_id=20,
        )

        selects = {
            item.kind: item
            for item in view.children
            if isinstance(item, sgbureau.CaseCreationMemberSelect)
        }
        self.assertEqual(set(selects), {"lawyer", "secretary"})
        self.assertEqual(selects["secretary"].placeholder, "Выберите секретаря")
        self.assertEqual(selects["secretary"].min_values, 0)
        self.assertEqual(selects["secretary"].max_values, 1)
        self.assertIn(
            "Без секретаря",
            [getattr(item, "label", None) for item in view.children],
        )

    async def test_selected_secretary_is_forwarded_to_case_creation(self):
        client = SimpleNamespace(id=20, display_name="Client")
        lawyer = SimpleNamespace(id=30, display_name="Lawyer")
        secretary = SimpleNamespace(id=40, display_name="Secretary")
        creator = SimpleNamespace(id=10, display_name="Creator")
        guild = FakeGuild(client, lawyer, secretary)
        expected = (
            SimpleNamespace(case_number=1),
            SimpleNamespace(id=50),
        )

        with patch.object(
            sgbureau,
            "create_sgl_case_channel",
            new=AsyncMock(return_value=expected),
        ) as create_case:
            result = await sgbureau.create_sgl_case_from_selection(
                bot=SimpleNamespace(),
                guild=guild,
                created_by=creator,
                client_id=client.id,
                lawyer_id=lawyer.id,
                secretary_id=secretary.id,
            )

        self.assertEqual(result, expected)
        create_case.assert_awaited_once()
        call = create_case.await_args.kwargs
        self.assertIs(call["client"], client)
        self.assertIs(call["lawyer"], lawyer)
        self.assertIs(call["secretary"], secretary)

    async def test_case_creation_still_allows_no_secretary(self):
        client = SimpleNamespace(id=20, display_name="Client")
        lawyer = SimpleNamespace(id=30, display_name="Lawyer")
        creator = SimpleNamespace(id=10, display_name="Creator")
        guild = FakeGuild(client, lawyer)

        with patch.object(
            sgbureau,
            "create_sgl_case_channel",
            new=AsyncMock(
                return_value=(
                    SimpleNamespace(case_number=1),
                    SimpleNamespace(id=50),
                )
            ),
        ) as create_case:
            await sgbureau.create_sgl_case_from_selection(
                bot=SimpleNamespace(),
                guild=guild,
                created_by=creator,
                client_id=client.id,
                lawyer_id=lawyer.id,
                secretary_id=None,
            )

        self.assertIsNone(create_case.await_args.kwargs["secretary"])


if __name__ == "__main__":
    unittest.main()
