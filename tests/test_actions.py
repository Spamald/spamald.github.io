import tempfile
import unittest
from pathlib import Path

from agent.actions import ActionError, FileActionExecutor


class FileActionExecutorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.executor = FileActionExecutor(self.root)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_lists_entries_in_case_insensitive_name_order(self):
        (self.root / "zeta.txt").write_text("123", encoding="utf-8")
        (self.root / "Alpha.txt").write_text("1", encoding="utf-8")
        result = self.executor.execute({"action": "list_files", "path": "."})
        self.assertEqual(
            [entry["name"] for entry in result["entries"]],
            ["Alpha.txt", "zeta.txt"],
        )
        self.assertEqual(result["base_folder"], str(self.root))

    def test_list_reports_absolute_path_of_requested_subfolder(self):
        subfolder = self.root / "subfolder"
        subfolder.mkdir()
        result = self.executor.execute(
            {"action": "list_files", "path": "subfolder"}
        )
        self.assertEqual(result["base_folder"], str(subfolder))

    def test_list_defaults_to_managed_root_and_accepts_root_name_prefix(self):
        (self.root / "sample.txt").write_text("x", encoding="utf-8")
        default_result = self.executor.execute({"action": "list_files"})
        prefixed_result = self.executor.execute(
            {"action": "list_files", "path": f"{self.root.name}"}
        )
        self.assertEqual(default_result["entries"], prefixed_result["entries"])
        self.assertEqual(default_result["entries"][0]["name"], "sample.txt")

    def test_get_info_returns_file_name_and_size(self):
        (self.root / "sample.txt").write_text("hello", encoding="utf-8")
        result = self.executor.execute({"action": "get_info", "path": "sample.txt"})
        self.assertEqual(result["name"], "sample.txt")
        self.assertEqual(result["size_bytes"], 5)

    def test_create_file_and_nested_folder(self):
        folder = self.executor.execute(
            {"action": "create_folder", "path": "demo_folder/A4"}
        )
        file = self.executor.execute(
            {"action": "create_file", "path": "demo_folder/A4/notes.txt"}
        )
        self.assertEqual(folder["created"], "demo_folder/A4")
        self.assertEqual(file["created"], "demo_folder/A4/notes.txt")
        self.assertTrue((self.root / "demo_folder" / "A4" / "notes.txt").is_file())
        self.assertEqual((self.root / "demo_folder" / "A4" / "notes.txt").stat().st_size, 0)

    def test_create_refuses_existing_paths_and_missing_file_parent(self):
        (self.root / "existing.txt").write_text("keep", encoding="utf-8")
        (self.root / "archive").mkdir()
        with self.assertRaisesRegex(ActionError, "already exists"):
            self.executor.execute({"action": "create_file", "path": "existing.txt"})
        with self.assertRaisesRegex(ActionError, "already exists"):
            self.executor.execute({"action": "create_folder", "path": "archive"})
        with self.assertRaisesRegex(ActionError, "parent directory does not exist"):
            self.executor.execute({"action": "create_file", "path": "missing/file.txt"})
        self.assertEqual((self.root / "existing.txt").read_text(encoding="utf-8"), "keep")

    def test_rename_and_move_file(self):
        (self.root / "source.txt").write_text("data", encoding="utf-8")
        (self.root / "subdir").mkdir()
        renamed = self.executor.execute(
            {"action": "rename_file", "path": "source.txt", "new_name": "renamed.txt"}
        )
        self.assertEqual(renamed["to"], "renamed.txt")
        moved = self.executor.execute(
            {
                "action": "move_file",
                "path": "renamed.txt",
                "destination": "subdir/final.txt",
            }
        )
        self.assertEqual(moved["to"], "subdir/final.txt")
        self.assertTrue((self.root / "subdir" / "final.txt").is_file())

    def test_file_actions_resolve_unique_extensionless_name(self):
        source = self.root / "notes.txt"
        source.write_text("notes", encoding="utf-8")
        info = self.executor.execute({"action": "get_info", "path": "notes"})
        self.assertEqual(info["name"], "notes.txt")
        result = self.executor.execute(
            {"action": "rename_file", "path": "notes", "new_name": "note"}
        )
        self.assertEqual(result["to"], "note")
        self.assertTrue((self.root / "note").is_file())

    def test_move_resolves_nested_source_and_directory_destination(self):
        archive = self.root / "archive"
        archive.mkdir()
        source = archive / "old-report.txt"
        source.write_text("report", encoding="utf-8")
        destination = self.root / "inbox"
        destination.mkdir()

        result = self.executor.execute(
            {
                "action": "move_file",
                "path": "old-report.txt",
                "destination": "inbox",
            }
        )

        self.assertEqual(result["from"], "archive/old-report.txt")
        self.assertEqual(result["to"], "inbox/old-report.txt")
        self.assertTrue((destination / source.name).is_file())
        self.assertFalse(source.exists())

    def test_move_to_managed_root_uses_root_name_alias(self):
        archive = self.root / "archive"
        archive.mkdir()
        (archive / "old-report.txt").write_text("report", encoding="utf-8")

        result = self.executor.execute(
            {
                "action": "move_file",
                "path": "old-report.txt",
                "destination": self.root.name,
            }
        )

        self.assertEqual(result["to"], "old-report.txt")
        self.assertTrue((self.root / "old-report.txt").is_file())

    def test_extensionless_name_must_match_only_one_file(self):
        (self.root / "notes.txt").write_text("1", encoding="utf-8")
        (self.root / "notes.md").write_text("2", encoding="utf-8")
        with self.assertRaisesRegex(ActionError, "Ambiguous file name"):
            self.executor.execute({"action": "get_info", "path": "notes"})

    def test_rename_folder_and_remove_file(self):
        folder = self.root / "old"
        folder.mkdir()
        (folder / "file.txt").write_text("x", encoding="utf-8")
        self.executor.execute(
            {"action": "rename_folder", "path": "old", "new_name": "new"}
        )
        self.executor.execute({"action": "remove_file", "path": "new/file.txt"})
        self.assertFalse((self.root / "new" / "file.txt").exists())

    def test_rejects_traversal_and_absolute_paths(self):
        for path in ("../outside.txt", str(self.root / "outside.txt")):
            with self.subTest(path=path), self.assertRaises(ActionError):
                self.executor.execute({"action": "get_info", "path": path})

    def test_rejects_overwrite_and_directory_removal(self):
        (self.root / "one.txt").write_text("1", encoding="utf-8")
        (self.root / "two.txt").write_text("2", encoding="utf-8")
        with self.assertRaises(ActionError):
            self.executor.execute(
                {"action": "rename_file", "path": "one.txt", "new_name": "two.txt"}
            )
        with self.assertRaises(ActionError):
            self.executor.execute({"action": "remove_file", "path": "."})

    def test_rejects_extra_arguments(self):
        with self.assertRaises(ActionError):
            self.executor.execute(
                {"action": "remove_file", "path": "x", "execute_shell": "true"}
            )
        with self.assertRaises(ActionError):
            self.executor.execute({"action": [], "path": "."})

    def test_write_actions_require_confirmation(self):
        self.assertTrue(
            self.executor.requires_confirmation({"action": "remove_file"})
        )
        self.assertTrue(
            self.executor.requires_confirmation({"action": "create_file"})
        )
        self.assertTrue(
            self.executor.requires_confirmation({"action": "create_folder"})
        )
        self.assertFalse(
            self.executor.requires_confirmation({"action": "list_files"})
        )


if __name__ == "__main__":
    unittest.main()
