import tempfile
import unittest
from pathlib import Path

from agent.actions import FileActionExecutor
from agent.dsl import DSLCancelled, DSLExecutionError, DSLExecutor, DSLParseError


class DSLExecutorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        (self.root / "notes.txt").write_text("notes", encoding="utf-8")
        (self.root / "archive").mkdir()
        (self.root / "archive" / "old-report.txt").write_text("report", encoding="utf-8")
        self.executor = DSLExecutor(FileActionExecutor(self.root))
        self.events = []

    def tearDown(self):
        self.temp_dir.cleanup()

    def audit(self, **fields):
        self.events.append(fields)

    def test_parses_all_commands_and_quoted_paths(self):
        commands = self.executor.parse(
            """
            # Comment
            LIST
            LIST "folder with spaces"
            INFO "notes.txt"
            CREATE_FILE "new file.txt"
            CREATE_FOLDER "new folder/A4"
            RENAME_FILE "notes.txt" TO "new notes.txt"
            RENAME_FOLDER archive TO old
            MOVE_FILE "old report.txt" TO archive
            REMOVE_FILE "new notes.txt"
            """
        )
        self.assertEqual(len(commands), 9)
        self.assertEqual(commands[0].action, {"action": "list_files", "path": "."})
        self.assertEqual(
            commands[3].action,
            {"action": "create_file", "path": "new file.txt"},
        )
        self.assertEqual(
            commands[5].action,
            {
                "action": "rename_file",
                "path": "notes.txt",
                "new_name": "new notes.txt",
            },
        )

    def test_parse_errors_report_source_line(self):
        with self.assertRaisesRegex(DSLParseError, "Line 2: Usage: INFO"):
            self.executor.parse("\nINFO")

    def test_validates_entire_program_before_running_any_command(self):
        script = "REMOVE_FILE notes.txt\nUNKNOWN notes.txt\n"
        with self.assertRaises(DSLParseError):
            self.executor.execute(script, lambda _action: True, self.audit)
        self.assertTrue((self.root / "notes.txt").exists())
        self.assertEqual(self.events, [])

    def test_executes_commands_in_order_with_confirmation_and_audit(self):
        script = """
        LIST
        INFO notes.txt
        CREATE_FOLDER created/nested
        CREATE_FILE created/nested/new.txt
        RENAME_FILE notes.txt TO note.txt
        RENAME_FOLDER archive TO old
        MOVE_FILE old-report.txt TO .
        REMOVE_FILE old-report.txt
        REMOVE_FILE note.txt
        """
        confirmations = []
        results = self.executor.execute(
            script,
            lambda action: confirmations.append(action["action"]) or True,
            self.audit,
        )
        self.assertEqual(len(results), 9)
        self.assertEqual(
            confirmations,
            [
                "create_folder",
                "create_file",
                "rename_file",
                "rename_folder",
                "move_file",
                "remove_file",
                "remove_file",
            ],
        )
        self.assertTrue((self.root / "created" / "nested" / "new.txt").is_file())
        self.assertFalse((self.root / "note.txt").exists())
        self.assertFalse((self.root / "old-report.txt").exists())
        self.assertTrue((self.root / "old").is_dir())
        self.assertEqual(
            sum(event["event"] == "dsl_action_completed" for event in self.events),
            9,
        )

    def test_declining_write_cancels_remaining_commands(self):
        script = "REMOVE_FILE notes.txt\nLIST\n"
        with self.assertRaises(DSLCancelled):
            self.executor.execute(script, lambda _action: False, self.audit)
        self.assertTrue((self.root / "notes.txt").exists())
        self.assertFalse(
            any(event["event"] == "dsl_action_completed" for event in self.events)
        )

    def test_action_failure_reports_line_and_stops_program(self):
        script = "LIST\nREMOVE_FILE missing.txt\nLIST\n"
        with self.assertRaisesRegex(DSLExecutionError, "Line 2: File does not exist"):
            self.executor.execute(script, lambda _action: True, self.audit)
        self.assertEqual(
            sum(event["event"] == "dsl_action_completed" for event in self.events),
            1,
        )


if __name__ == "__main__":
    unittest.main()
