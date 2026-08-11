import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from modules.consensus_artifacts import ensure_session_artifacts
from modules.consensus_core import LiveConsensusSession, LiveParticipant, LiveResult


class ConsensusArtifactTests(unittest.TestCase):
    def test_generates_publishable_cards_cover_and_pdf(self) -> None:
        participant = LiveParticipant(
            user_id=11,
            display_name="Сенатор Тестовый",
            mention="<@11>",
            kind="senator",
            confirmed=True,
        )
        result = LiveResult(
            bill_id=74,
            bill_number=74,
            title="О развитии общественных игр Товарищества",
            status="accepted",
            internal_percent=75.0,
            overall_percent=68.75,
            internal_active=True,
            votes={11: "yes"},
            required_percent=50.0,
            opposed_percent=0.0,
            block_votes={
                "first": "yes",
                "second": "yes",
                "third": "abstain",
                "consensus": "yes",
            },
        )
        session = LiveConsensusSession(
            session_key="artifact-test-session",
            guild_id=77,
            channel_id=88,
            leader_id=1,
            leader_display="Ведущий Тестовый",
            plenary_number=13,
            participants={11: participant},
            results=[result],
            stage="finished",
            finished=True,
        )
        bill = {
            "author_display": "Автор Тестовый",
            "summary": "Полный текст предложения для итогового протокола.",
            "materials": "https://example.com/material",
        }

        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ,
            {"CONSENSUS_ARTIFACTS_DIR": temp_dir},
        ), patch(
            "modules.consensus_artifacts.storage.tvrs_get_bill_dict_by_id",
            return_value=bill,
        ):
            artifacts = ensure_session_artifacts(session, force=True)

            self.assertEqual(len(artifacts["cards"]), 1)
            for key in ("pdf", "cover"):
                self.assertTrue(Path(artifacts[key]).is_file())
            self.assertTrue(Path(artifacts["cards"][0]).is_file())
            self.assertGreater(Path(artifacts["pdf"]).stat().st_size, 1_000)
            self.assertEqual(Path(artifacts["pdf"]).read_bytes()[:4], b"%PDF")
            with Image.open(artifacts["cover"]) as cover:
                self.assertEqual(cover.size, (1600, 900))
            with Image.open(artifacts["cards"][0]) as card:
                self.assertEqual(card.size, (1600, 900))


if __name__ == "__main__":
    unittest.main()
