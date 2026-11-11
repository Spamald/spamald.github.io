"""Metadata inventory and validation for Groq-generated organization plans."""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from .actions import ActionError, FileActionExecutor
from .dsl import DSLCommand, DSLExecutor, DSLParseError


ORGANIZATION_DSL_SPEC = """File-manager DSL specification:
One command per line. Blank lines and lines beginning with # are ignored.
Paths are relative to the managed root, use forward slashes, and must be quoted
if they contain spaces.

Allowed commands for this organization plan:
CREATE_FOLDER <path>
MOVE_FILE <source-file-path> TO <existing-or-created-folder-path>

Do not use any other command. Reuse suitable folders already present in the
inventory; do not try to create an existing folder. Create each new destination
folder before moving files into it. Use exact relative paths for source files
as provided in the inventory. Do not use absolute paths, '..', or the managed
root's name as a path prefix. Every inventoried regular file must be moved
exactly once to a folder organized by its extension/name and metadata. No file
contents are provided, so do not claim content-based classification. Preserve
all files; never delete or rename them. Return only DSL commands, without
markdown fences or commentary."""


def build_inventory(file_manager: FileActionExecutor) -> dict[str, Any]:
    """Build detailed metadata by recursively listing directories via file manager."""
    root = file_manager.root
    pending = deque(["."])
    folder_records: dict[str, dict[str, Any]] = {}
    files: list[dict[str, Any]] = []
    skipped_symlinks: list[str] = []

    while pending:
        relative_folder = pending.popleft()
        listing = file_manager.execute(
            {"action": "list_files", "path": relative_folder}
        )
        folder_path = Path(str(listing["base_folder"]))
        folder_records[relative_folder] = {
            "path": relative_folder,
            "full_path": str(folder_path),
            "direct_file_count": 0,
            "direct_folder_count": 0,
            "total_file_count": 0,
            "total_size_bytes": 0,
        }

        for entry in listing["entries"]:
            name = entry["name"]
            child_relative = (
                name
                if relative_folder == "."
                else f"{relative_folder}/{name}"
            )
            if entry["type"] == "symlink":
                skipped_symlinks.append(child_relative)
            elif entry["type"] == "directory":
                folder_records[relative_folder]["direct_folder_count"] += 1
                pending.append(child_relative)
            elif entry["type"] == "file":
                full_path = folder_path / name
                stat = full_path.stat()
                suffix = full_path.suffix.lower()
                files.append(
                    {
                        "path": child_relative,
                        "full_path": str(full_path),
                        "name": name,
                        "extension": suffix or "(none)",
                        "size_bytes": int(entry["size_bytes"]),
                        "modified_utc": datetime.fromtimestamp(
                            stat.st_mtime, timezone.utc
                        ).isoformat(),
                    }
                )
                folder_records[relative_folder]["direct_file_count"] += 1
                folder_records[relative_folder]["total_file_count"] += 1
                folder_records[relative_folder]["total_size_bytes"] += int(
                    entry["size_bytes"]
                )

    for folder in sorted(folder_records, key=lambda value: len(Path(value).parts), reverse=True):
        if folder == ".":
            continue
        parent = Path(folder).parent.as_posix()
        if parent == "":
            parent = "."
        folder_records[parent]["total_file_count"] += folder_records[folder][
            "total_file_count"
        ]
        folder_records[parent]["total_size_bytes"] += folder_records[folder][
            "total_size_bytes"
        ]

    return {
        "managed_root": str(root),
        "folders": sorted(
            folder_records.values(), key=lambda record: record["path"].casefold()
        ),
        "files": sorted(files, key=lambda record: record["path"].casefold()),
        "skipped_symlinks": sorted(skipped_symlinks, key=str.casefold),
    }


def validate_organization_plan(
    source: str,
    file_manager: FileActionExecutor,
    inventory: dict[str, Any],
) -> list[DSLCommand]:
    """Ensure generated DSL only creates folders and moves each known file once."""
    dsl = DSLExecutor(file_manager)
    commands = dsl.parse(source)
    files = {item["path"] for item in inventory["files"]}
    folders = {item["path"] for item in inventory["folders"]}
    created_folders: set[str] = set()
    moved_files: set[str] = set()

    for command in commands:
        action = command.action
        action_name = action["action"]
        if action_name not in {"create_folder", "move_file"}:
            raise DSLParseError(
                command.line_number,
                "organization plans may only create folders and move files.",
            )
        try:
            file_manager.validate(action)
        except ActionError as exc:
            raise DSLParseError(command.line_number, str(exc)) from exc

        if action_name == "create_folder":
            folder = _normalize_plan_path(action["path"], command.line_number)
            _reject_root_prefix(folder, file_manager.root.name, command.line_number)
            if folder == "." or folder in folders or folder in created_folders:
                raise DSLParseError(
                    command.line_number,
                    f"folder already exists or is duplicated: {folder}",
                )
            if folder in files:
                raise DSLParseError(
                    command.line_number, f"folder path conflicts with file: {folder}"
                )
            created_folders.add(folder)
            continue

        source_path = _normalize_plan_path(action["path"], command.line_number)
        destination = _normalize_plan_path(
            action["destination"], command.line_number, allow_root=True
        )
        _reject_root_prefix(destination, file_manager.root.name, command.line_number)
        if source_path not in files:
            raise DSLParseError(
                command.line_number,
                f"source file is not in the inventory: {source_path}",
            )
        if source_path in moved_files:
            raise DSLParseError(
                command.line_number, f"file is moved more than once: {source_path}"
            )
        if destination not in folders and destination not in created_folders:
            raise DSLParseError(
                command.line_number,
                f"destination folder must exist or be created earlier: {destination}",
            )
        moved_files.add(source_path)

    missing_files = files - moved_files
    if missing_files:
        missing = ", ".join(sorted(missing_files, key=str.casefold))
        raise DSLParseError(
            commands[-1].line_number if commands else 1,
            f"plan does not move every inventoried file: {missing}",
        )
    return commands


def _normalize_plan_path(
    value: str, line_number: int, *, allow_root: bool = False
) -> str:
    if "\\" in value:
        raise DSLParseError(line_number, "use forward slashes in relative paths.")
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if posix.is_absolute() or windows.is_absolute() or windows.drive:
        raise DSLParseError(line_number, "absolute paths are not allowed.")
    if ".." in posix.parts or ".." in windows.parts:
        raise DSLParseError(line_number, "parent traversal is not allowed.")
    normalized = posix.as_posix()
    if normalized == "." and allow_root:
        return normalized
    if normalized in ("", "."):
        raise DSLParseError(line_number, "a relative path is required.")
    return normalized


def _reject_root_prefix(path: str, root_name: str, line_number: int) -> None:
    if path != "." and PurePosixPath(path).parts[0].casefold() == root_name.casefold():
        raise DSLParseError(
            line_number, "paths are relative to the managed root; do not prefix its name."
        )
