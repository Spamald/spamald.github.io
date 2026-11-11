import json
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from agent.audit import AuditLog
from agent.cli import run, run_dsl, run_organizer


class StubClient:
    def plan(self, _request):
        return {"action": "remove_file", "path": "keep.txt"}


class StubOrganizerClient:
    def generate_organization_dsl(self, _inventory, _specification):
        return 'CREATE_FOLDER "text"\nMOVE_FILE "notes.txt" TO "text"'


class CliTests(unittest.TestCase):
    def test_declined_write_is_logged_and_not_executed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "keep.txt"
            target.write_text("safe", encoding="utf-8")
            audit_path = root / "audit.jsonl"

            with (
                patch("builtins.input", side_effect=["remove keep.txt", "no", "exit"]),
                patch("sys.stdout", new_callable=StringIO),
                patch("sys.stderr", new_callable=StringIO),
            ):
                status = run(root, StubClient(), AuditLog(audit_path))

            self.assertEqual(status, 0)
            self.assertTrue(target.is_file())
            events = [
                json.loads(line)["event"]
                for line in audit_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                events,
                ["request", "proposed_action", "action_cancelled"],
            )

    def test_dsl_cli_executes_without_llm_and_logs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "notes.txt"
            target.write_text("notes", encoding="utf-8")
            script = root / "commands.fma"
            script.write_text("RENAME_FILE notes.txt TO note.txt\n", encoding="utf-8")
            audit_path = root / "audit.jsonl"

            with (
                patch("builtins.input", return_value="yes"),
                patch("sys.stdout", new_callable=StringIO),
                patch("sys.stderr", new_callable=StringIO),
            ):
                status = run_dsl(root, script, AuditLog(audit_path))

            self.assertEqual(status, 0)
            self.assertTrue((root / "note.txt").is_file())
            events = [
                json.loads(line)["event"]
                for line in audit_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                events,
                [
                    "dsl_program_started",
                    "dsl_action_proposed",
                    "dsl_action_completed",
                    "dsl_program_completed",
                ],
            )

    def test_organize_prompts_for_base_and_confirms_each_change(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "notes.txt").write_text("notes", encoding="utf-8")
            audit_path = root.parent / f"{root.name}-audit.jsonl"

            try:
                with (
                    patch(
                        "builtins.input",
                        side_effect=[str(root), "yes", "yes", "yes"],
                    ),
                    patch("sys.stdout", new_callable=StringIO),
                    patch("sys.stderr", new_callable=StringIO),
                ):
                    status = run_organizer(
                        StubOrganizerClient(), AuditLog(audit_path)
                    )

                self.assertEqual(status, 0)
                self.assertTrue((root / "text" / "notes.txt").is_file())
                events = [
                    json.loads(line)["event"]
                    for line in audit_path.read_text(encoding="utf-8").splitlines()
                ]
                self.assertIn("organize_inventory_sent", events)
                self.assertIn("organize_plan_generated", events)
                self.assertIn("organize_completed", events)
            finally:
                audit_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
