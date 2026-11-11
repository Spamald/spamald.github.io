import tempfile
import unittest
from pathlib import Path

from agent.actions import FileActionExecutor
from agent.dsl import DSLParseError
from agent.organizer import build_inventory, validate_organization_plan


class OrganizationInventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        (self.root / "notes.txt").write_text("private contents", encoding="utf-8")
        (self.root / "images").mkdir()
        (self.root / "images" / "photo.png").write_bytes(b"not sent as contents")
        self.file_manager = FileActionExecutor(self.root)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_inventory_contains_recursive_file_and_folder_metadata_only(self):
        inventory = build_inventory(self.file_manager)
        self.assertEqual(inventory["managed_root"], str(self.root))
        files = {item["path"]: item for item in inventory["files"]}
        self.assertEqual(set(files), {"notes.txt", "images/photo.png"})
        self.assertEqual(files["notes.txt"]["extension"], ".txt")
        self.assertEqual(files["notes.txt"]["size_bytes"], len("private contents"))
        self.assertNotIn("contents", files["notes.txt"])
        folders = {item["path"]: item for item in inventory["folders"]}
        self.assertEqual(folders["."]["total_file_count"], 2)
        self.assertEqual(folders["."]["direct_folder_count"], 1)
        self.assertEqual(folders["images"]["total_file_count"], 1)

    def test_plan_must_move_each_file_once_to_a_known_folder(self):
        inventory = build_inventory(self.file_manager)
        source = (
            'CREATE_FOLDER "text"\n'
            'CREATE_FOLDER "images-group"\n'
            'MOVE_FILE "notes.txt" TO "text"\n'
            'MOVE_FILE "images/photo.png" TO "images-group"\n'
        )
        commands = validate_organization_plan(source, self.file_manager, inventory)
        self.assertEqual(len(commands), 4)

    def test_plan_rejects_unsupported_commands(self):
        inventory = build_inventory(self.file_manager)
        source = (
            'CREATE_FOLDER "text"\n'
            'MOVE_FILE "notes.txt" TO "text"\n'
            'REMOVE_FILE "images/photo.png"\n'
        )
        with self.assertRaisesRegex(DSLParseError, "only create folders and move files"):
            validate_organization_plan(source, self.file_manager, inventory)

    def test_plan_rejects_missing_duplicate_or_unknown_sources(self):
        inventory = build_inventory(self.file_manager)
        with self.assertRaisesRegex(DSLParseError, "does not move every"):
            validate_organization_plan(
                'CREATE_FOLDER "text"\nMOVE_FILE "notes.txt" TO "text"\n',
                self.file_manager,
                inventory,
            )
        duplicate = (
            'CREATE_FOLDER "text"\n'
            'MOVE_FILE "notes.txt" TO "text"\n'
            'MOVE_FILE "notes.txt" TO "text"\n'
            'MOVE_FILE "images/photo.png" TO "text"\n'
        )
        with self.assertRaisesRegex(DSLParseError, "moved more than once"):
            validate_organization_plan(duplicate, self.file_manager, inventory)

    def test_plan_rejects_unsafe_paths_and_uncreated_destination(self):
        inventory = build_inventory(self.file_manager)
        with self.assertRaisesRegex(DSLParseError, "parent traversal"):
            validate_organization_plan(
                'CREATE_FOLDER "../escape"\n'
                'MOVE_FILE "notes.txt" TO "../escape"\n'
                'MOVE_FILE "images/photo.png" TO "../escape"\n',
                self.file_manager,
                inventory,
            )
        with self.assertRaisesRegex(DSLParseError, "must exist or be created earlier"):
            validate_organization_plan(
                'MOVE_FILE "notes.txt" TO "missing"\n'
                'MOVE_FILE "images/photo.png" TO "missing"\n',
                self.file_manager,
                inventory,
            )

    def test_plan_rejects_root_name_prefix(self):
        inventory = build_inventory(self.file_manager)
        with self.assertRaisesRegex(DSLParseError, "do not prefix its name"):
            validate_organization_plan(
                f'CREATE_FOLDER "{self.root.name}/text"\n',
                self.file_manager,
                inventory,
            )

    def test_plan_rejects_case_mismatched_destination_and_unsafe_names(self):
        inventory = build_inventory(self.file_manager)
        with self.assertRaisesRegex(DSLParseError, "casing does not match"):
            validate_organization_plan(
                'MOVE_FILE "notes.txt" TO "Images"\n'
                'MOVE_FILE "images/photo.png" TO "Images"\n',
                self.file_manager,
                inventory,
            )
        with self.assertRaisesRegex(DSLParseError, "unsupported path component"):
            validate_organization_plan(
                'CREATE_FOLDER "CON"\n'
                'MOVE_FILE "notes.txt" TO "CON"\n'
                'MOVE_FILE "images/photo.png" TO "CON"\n',
                self.file_manager,
                inventory,
            )


if __name__ == "__main__":
    unittest.main()
