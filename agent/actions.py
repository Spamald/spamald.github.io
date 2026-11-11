"""Validation and execution for the agent's explicitly supported file actions."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any


class ActionError(ValueError):
    """Raised when an action is invalid or cannot safely be executed."""


READ_ACTIONS = {"list_files", "get_info"}
WRITE_ACTIONS = {
    "create_file",
    "create_folder",
    "rename_file",
    "rename_folder",
    "move_file",
    "remove_file",
}
SUPPORTED_ACTIONS = READ_ACTIONS | WRITE_ACTIONS


class FileActionExecutor:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).expanduser().resolve()
        if not self.root.is_dir():
            raise ActionError(f"Managed root is not a directory: {self.root}")

    @staticmethod
    def requires_confirmation(action: dict[str, Any]) -> bool:
        return action.get("action") in WRITE_ACTIONS

    def validate(self, action: dict[str, Any]) -> dict[str, str]:
        if not isinstance(action, dict):
            raise ActionError("Action must be a JSON object.")
        name = action.get("action")
        if name == "unsupported":
            reason = action.get("reason", "That request is not supported.")
            if not isinstance(reason, str):
                raise ActionError("Unsupported-action reason must be text.")
            raise ActionError(reason)
        if not isinstance(name, str):
            raise ActionError("Action name must be text.")
        if name not in SUPPORTED_ACTIONS:
            raise ActionError(f"Unsupported action: {name!r}")

        required = {
            "list_files": {"action", "path"},
            "get_info": {"action", "path"},
            "create_file": {"action", "path"},
            "create_folder": {"action", "path"},
            "rename_file": {"action", "path", "new_name"},
            "rename_folder": {"action", "path", "new_name"},
            "move_file": {"action", "path", "destination"},
            "remove_file": {"action", "path"},
        }[name]
        if name == "list_files" and "path" not in action:
            action = {**action, "path": "."}
        if set(action) != required:
            missing = required - set(action)
            extra = set(action) - required
            details = []
            if missing:
                details.append(f"missing {', '.join(sorted(missing))}")
            if extra:
                details.append(f"unexpected {', '.join(sorted(extra))}")
            raise ActionError(f"Invalid {name} arguments: {'; '.join(details)}.")
        if any(not isinstance(value, str) or not value.strip() for key, value in action.items() if key != "action"):
            raise ActionError("Action arguments must be non-empty strings.")
        return action

    def execute(self, raw_action: dict[str, Any]) -> dict[str, object]:
        action = self.validate(raw_action)
        name = action["action"]
        if name == "list_files":
            return self._list_files(action["path"])
        if name == "get_info":
            return self._get_info(action["path"])
        if name == "create_file":
            return self._create_file(action["path"])
        if name == "create_folder":
            return self._create_folder(action["path"])
        if name == "rename_file":
            return self._rename(action["path"], action["new_name"], is_folder=False)
        if name == "rename_folder":
            return self._rename(action["path"], action["new_name"], is_folder=True)
        if name == "move_file":
            return self._move_file(action["path"], action["destination"])
        return self._remove_file(action["path"])

    def _create_file(self, raw_path: str) -> dict[str, object]:
        path = self._resolve(raw_path, must_exist=False)
        if path == self.root:
            raise ActionError("A file path must name a file inside the managed root.")
        if not path.parent.is_dir():
            raise ActionError("The parent directory does not exist.")
        try:
            with path.open("x", encoding="utf-8"):
                pass
        except FileExistsError as exc:
            raise ActionError(f"Destination already exists: {raw_path}") from exc
        return {
            "action": "create_file",
            "created": path.relative_to(self.root).as_posix(),
        }

    def _create_folder(self, raw_path: str) -> dict[str, object]:
        path = self._resolve(raw_path, must_exist=False)
        if path == self.root:
            raise ActionError("The managed root already exists.")
        if path.exists() or path.is_symlink():
            raise ActionError(f"Destination already exists: {raw_path}")
        try:
            path.mkdir(parents=True, exist_ok=False)
        except FileExistsError as exc:
            raise ActionError(f"Destination already exists: {raw_path}") from exc
        return {
            "action": "create_folder",
            "created": path.relative_to(self.root).as_posix(),
        }

    def _resolve(self, raw_path: str, *, must_exist: bool) -> Path:
        relative = Path(raw_path)
        if relative.is_absolute() or relative.drive or relative.root or ".." in relative.parts:
            raise ActionError("Paths must stay relative to the managed root.")
        parts = relative.parts
        if parts and parts[0].casefold() == self.root.name.casefold():
            parts = parts[1:]
        candidate = self.root
        for part in parts:
            if part in ("", "."):
                continue
            candidate = candidate / part
            if candidate.is_symlink():
                raise ActionError("Symbolic links are not allowed.")
        resolved = candidate.resolve(strict=must_exist)
        if not resolved.is_relative_to(self.root):
            raise ActionError("The path is outside the managed root.")
        if must_exist and not resolved.exists():
            raise ActionError(f"Path does not exist: {raw_path}")
        return resolved

    def _resolve_file(self, raw_path: str) -> Path:
        candidate = self._resolve(raw_path, must_exist=False)
        if candidate.exists():
            return self._resolve(raw_path, must_exist=True)
        matches: list[Path] = []
        for current, directories, filenames in os.walk(self.root, followlinks=False):
            current_path = Path(current)
            directories[:] = [
                name
                for name in directories
                if not (current_path / name).is_symlink()
            ]
            for filename in filenames:
                item = current_path / filename
                if item.is_symlink():
                    continue
                if filename.casefold() == candidate.name.casefold() or (
                    not candidate.suffix
                    and item.stem.casefold() == candidate.name.casefold()
                ):
                    matches.append(item)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            choices = ", ".join(
                sorted(item.relative_to(self.root).as_posix() for item in matches)
            )
            raise ActionError(f"Ambiguous file name {raw_path!r}; matches: {choices}")
        raise ActionError(f"File does not exist: {raw_path}")

    def _list_files(self, raw_path: str) -> dict[str, object]:
        directory = self._resolve(raw_path, must_exist=True)
        if not directory.is_dir():
            raise ActionError(f"Not a directory: {raw_path}")
        entries = []
        for item in sorted(directory.iterdir(), key=lambda entry: (entry.name.casefold(), entry.name)):
            if item.is_symlink():
                kind = "symlink"
            else:
                kind = "directory" if item.is_dir() else "file"
            entries.append(
                {
                    "name": item.name,
                    "type": kind,
                    "size_bytes": item.stat().st_size if kind == "file" else None,
                }
            )
        return {
            "action": "list_files",
            "path": raw_path,
            "base_folder": str(directory),
            "entries": entries,
        }

    def _get_info(self, raw_path: str) -> dict[str, object]:
        path = self._resolve_file(raw_path)
        if not path.is_file():
            raise ActionError(f"Not a file: {raw_path}")
        return {"action": "get_info", "name": path.name, "size_bytes": path.stat().st_size}

    def _new_child(self, parent: Path, new_name: str) -> Path:
        name = Path(new_name)
        if name.name != new_name or new_name in ("", ".", "..") or name.is_absolute():
            raise ActionError("The new name must be a single filename, not a path.")
        destination = parent / new_name
        if destination.exists() or destination.is_symlink():
            raise ActionError(f"Destination already exists: {new_name}")
        return destination

    def _rename(self, raw_path: str, new_name: str, *, is_folder: bool) -> dict[str, object]:
        source = (
            self._resolve(raw_path, must_exist=True)
            if is_folder
            else self._resolve_file(raw_path)
        )
        if source.is_dir() != is_folder:
            kind = "folder" if is_folder else "file"
            raise ActionError(f"Not a {kind}: {raw_path}")
        destination = self._new_child(source.parent, new_name)
        source.rename(destination)
        return {
            "action": "rename_folder" if is_folder else "rename_file",
            "from": source.relative_to(self.root).as_posix(),
            "to": destination.relative_to(self.root).as_posix(),
        }

    def _move_file(self, raw_path: str, raw_destination: str) -> dict[str, object]:
        source = self._resolve_file(raw_path)
        if not source.is_file():
            raise ActionError(f"Not a file: {raw_path}")
        destination = self._resolve(raw_destination, must_exist=False)
        if destination.is_dir():
            destination = destination / source.name
        if destination.exists() or destination.is_symlink():
            raise ActionError(f"Destination already exists: {raw_destination}")
        if not destination.parent.is_dir():
            raise ActionError("The destination directory does not exist.")
        shutil.move(str(source), str(destination))
        return {
            "action": "move_file",
            "from": source.relative_to(self.root).as_posix(),
            "to": destination.relative_to(self.root).as_posix(),
        }

    def _remove_file(self, raw_path: str) -> dict[str, object]:
        path = self._resolve_file(raw_path)
        if not path.is_file():
            raise ActionError(f"Not a file: {raw_path}")
        path.unlink()
        return {
            "action": "remove_file",
            "removed": path.relative_to(self.root).as_posix(),
        }
