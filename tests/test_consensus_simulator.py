import ast
import asyncio
import unittest
from pathlib import Path

from modules.consensus_core import LiveParticipant
from modules.consensus_v3 import CONSENSUS_ENGINE_VERSION
from modules.consensus_simulator import (
    ConsensusSimulation,
    SimulationParticipantView,
    ConsensusSimulationView,
    InMemoryConsensusRepository,
    consensus_simulation_embed,
)
from modules.consensus_service import ConsensusCoordinator


ROOT = Path(__file__).resolve().parents[1]


class ConsensusSimulationTests(unittest.TestCase):
    def simulation(self) -> ConsensusSimulation:
        return ConsensusSimulation(
            guild_id=77,
            leader_id=100,
            leader_display="Учебный ведущий",
        )

    def test_full_training_lifecycle_uses_production_rules_without_voice(self) -> None:
        simulation = self.simulation()
        self.assertEqual(simulation.session.engine_version, CONSENSUS_ENGINE_VERSION)
        simulation.confirm_all()
        self.assertTrue(simulation.session.quorum_ready())
        simulation.begin_voting()
        self.assertEqual(simulation.session.stage, "presentation")
        preview = ConsensusSimulationView(simulation)
        preview_labels = {str(item.label) for item in preview.children}
        self.assertIn("Поставить на воут", preview_labels)
        self.assertNotIn("За", preview_labels)
        self.assertNotIn("Против", preview_labels)
        simulation.open_voting()
        simulation.set_timer(300)
        self.assertEqual(simulation.session.timer_seconds, 300)
        simulation.cast_leader_vote("yes")
        simulation.apply_fake_scenario("mixed")

        result = simulation.session.results[-1]
        self.assertEqual(result.internal_percent, 66.67)
        self.assertEqual(result.overall_percent, 75.0)
        self.assertEqual(result.status, "accepted")
        self.assertEqual(simulation.session.stage, "after_result")
        self.assertEqual(simulation.queue_bills(1)[0]["bill_number"], 901)

        simulation.next_bill()
        self.assertEqual(simulation.bill_number, 901)
        self.assertEqual(simulation.queue_bills(1)[0]["bill_number"], 902)
        simulation.pause()
        simulation.resume()
        simulation.open_voting()
        simulation.request_discussion()
        simulation.choose_discussion("Правовая")
        simulation.end_discussion()
        simulation.finish()
        self.assertTrue(simulation.finished)

    def test_simulator_uses_production_coordinator_and_memory_only_repository(self) -> None:
        simulation = self.simulation()

        self.assertIsInstance(simulation.coordinator, ConsensusCoordinator)
        self.assertIsInstance(simulation.repository, InMemoryConsensusRepository)
        initial_revision = simulation.session.revision

        simulation.confirm_next()

        self.assertGreater(simulation.session.revision, initial_revision)
        snapshot = simulation.repository.snapshots[simulation.session.session_key]
        self.assertEqual(snapshot["revision"], simulation.session.revision)
        self.assertEqual(
            simulation.repository.events[-1]["event_type"],
            "participant_confirmed",
        )

    def test_oral_result_follows_finalization_pipeline(self) -> None:
        simulation = self.simulation()
        simulation.confirm_all()
        simulation.begin_voting()
        simulation.open_voting()
        simulation.request_discussion()
        simulation.choose_discussion("Правовая")

        result = simulation.oral_result("accepted")

        self.assertEqual(result.resolution_method, "oral")
        self.assertEqual(simulation.session.stage, "after_result")
        event_types = [event["event_type"] for event in simulation.repository.events]
        self.assertIn("veto_claimed", event_types)
        self.assertIn("oral_result_recorded", event_types)

    def test_veto_is_isolated_and_terminal_for_only_current_fake_bill(self) -> None:
        simulation = self.simulation()
        simulation.confirm_all()
        simulation.begin_voting()
        simulation.open_voting()
        result = simulation.veto()
        self.assertEqual(result.status, "vetoed")
        self.assertEqual(result.veto_by_id, 100)
        self.assertEqual(simulation.session.stage, "after_result")

    def test_simulator_has_no_storage_or_live_runtime_dependency(self) -> None:
        path = ROOT / "modules" / "consensus_simulator.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module)
        self.assertFalse(
            imports
            & {
                "storage",
                "persistence",
                "modules.consensus_runtime",
                "modules.consensus_repository",
            }
        )

    def test_real_roster_uses_real_registration_and_ballot_controls(self) -> None:
        invitees = (
            LiveParticipant(201, "Сенатор Один", "<@201>", "senator"),
            LiveParticipant(202, "Сенатор Два", "<@202>", "senator"),
        )
        simulation = ConsensusSimulation(
            guild_id=77,
            leader_id=100,
            leader_display="Ведущий",
            invited_participants=invitees,
        )

        self.assertEqual(set(simulation.session.participants), {100, 201, 202})
        self.assertFalse(simulation.session.participants[100].permanent)
        registration = ConsensusSimulationView(simulation)
        labels = {str(item.label) for item in registration.children}
        self.assertIn("Повторить приглашения", labels)
        self.assertNotIn("Подтвердить всех", labels)

        simulation.confirm_participant(201)
        simulation.confirm_participant(202)
        self.assertTrue(simulation.session.quorum_ready())
        simulation.begin_voting()
        preview = SimulationParticipantView(simulation, 201)
        self.assertFalse(preview.children)
        simulation.open_voting()
        ballot = SimulationParticipantView(simulation, 201)
        ballot_labels = {str(item.label) for item in ballot.children}
        self.assertEqual(
            {"За", "Против", "Воздержаться", "Дискуссия"},
            ballot_labels,
        )
        simulation.cast_participant_vote(201, "yes")
        self.assertEqual(simulation.session.votes[201], "yes")

    def test_every_stage_fits_discord_component_and_embed_limits(self) -> None:
        async def inspect() -> None:
            simulation = self.simulation()
            stages = [ConsensusSimulationView(simulation)]
            simulation.confirm_all()
            simulation.begin_voting()
            stages.append(ConsensusSimulationView(simulation))
            simulation.open_voting()
            stages.append(ConsensusSimulationView(simulation))
            simulation.request_discussion()
            stages.append(ConsensusSimulationView(simulation))
            simulation.choose_discussion("Иная")
            stages.append(ConsensusSimulationView(simulation))
            simulation.pause()
            stages.append(ConsensusSimulationView(simulation))
            simulation.resume()
            simulation.end_discussion()
            simulation.finalize()
            stages.append(ConsensusSimulationView(simulation))

            for view in stages:
                self.assertLessEqual(len(view.children), 25)
                row_counts: dict[int, int] = {}
                for item in view.children:
                    row = int(item.row or 0)
                    row_counts[row] = row_counts.get(row, 0) + 1
                self.assertTrue(all(count <= 5 for count in row_counts.values()))

            embed = consensus_simulation_embed(simulation)
            self.assertLessEqual(len(embed), 6000)
            self.assertTrue(all(len(field.value) <= 1024 for field in embed.fields))

        asyncio.run(inspect())


if __name__ == "__main__":
    unittest.main()
