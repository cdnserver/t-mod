import ast
import asyncio
import unittest
from pathlib import Path

from modules.consensus_core import calculate_consensus
from modules.consensus_simulator import (
    ConsensusSimulation,
    ConsensusSimulationView,
    consensus_simulation_embed,
)


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
        simulation.confirm_all()
        self.assertTrue(simulation.session.quorum_ready())
        simulation.begin_voting()
        simulation.cast_leader_vote("yes")
        simulation.apply_fake_scenario("mixed")

        calculation = calculate_consensus(simulation.session)
        self.assertEqual(calculation["internal_percent"], 66.67)
        self.assertEqual(calculation["overall_percent"], 51.0)
        result = simulation.finalize()
        self.assertEqual(result.status, "accepted")
        self.assertEqual(simulation.session.stage, "after_result")

        simulation.next_bill()
        self.assertEqual(simulation.bill_number, 901)
        simulation.pause()
        simulation.resume()
        simulation.request_discussion()
        simulation.choose_discussion("Правовая")
        simulation.end_discussion()
        simulation.finish()
        self.assertTrue(simulation.finished)

    def test_veto_is_isolated_and_terminal_for_only_current_fake_bill(self) -> None:
        simulation = self.simulation()
        simulation.confirm_all()
        simulation.begin_voting()
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

    def test_every_stage_fits_discord_component_and_embed_limits(self) -> None:
        async def inspect() -> None:
            simulation = self.simulation()
            stages = [ConsensusSimulationView(simulation)]
            simulation.confirm_all()
            simulation.begin_voting()
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
