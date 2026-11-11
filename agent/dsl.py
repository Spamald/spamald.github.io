"""Parser and executor for the file-agent DSL."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Any, Callable

from .actions import ActionError, FileActionExecutor


@dataclass(frozen=True)
class DSLCommand:
    line_number: int
    source: str
    action: dict[str, str]


class DSLParseError(ValueError):
    def __init__(self, line_number: int, message: str) -> None:
        self.line_number = line_number
        super().__init__(f"Line {line_number}: {message}")


class DSLExecutionError(RuntimeError):
    def __init__(self, line_number: int, message: str) -> None:
        self.line_number = line_number
        super().__init__(f"Line {line_number}: {message}")


class DSLCancelled(DSLExecutionError):
    pass


AuditCallback = Callable[..., None]
ConfirmationCallback = Callable[[dict[str, Any]], bool]


class DSLExecutor:
    """Runs one file-manager operation per non-empty DSL source line."""

    def __init__(self, file_executor: FileActionExecutor) -> None:
        self.file_executor = file_executor

    def parse(self, source: str) -> list[DSLCommand]:
        commands = []
        for line_number, raw_line in enumerate(source.splitlines(), start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                tokens = shlex.split(line, comments=False, posix=True)
            except ValueError as exc:
                raise DSLParseError(line_number, f"invalid quoting: {exc}") from exc
            if not tokens:
                continue
            action = self._parse_command(tokens, line_number)
            commands.append(DSLCommand(line_number, line, action))
        return commands

    def execute(
        self,
        source: str,
        confirm: ConfirmationCallback,
        audit: AuditCallback,
    ) -> list[dict[str, object]]:
        commands = self.parse(source)
        validated: list[tuple[DSLCommand, dict[str, str]]] = []
        for command in commands:
            try:
                action = self.file_executor.validate(command.action)
            except ActionError as exc:
                raise DSLExecutionError(command.line_number, str(exc)) from exc
            validated.append((command, action))

        results = []
        for command, action in validated:
            audit(
                event="dsl_action_proposed",
                line=command.line_number,
                command=command.source,
                action=action,
            )
            if self.file_executor.requires_confirmation(action) and not confirm(action):
                audit(
                    event="dsl_action_cancelled",
                    line=command.line_number,
                    command=command.source,
                    action=action,
                    result="User declined confirmation.",
                )
                raise DSLCancelled(command.line_number, "Cancelled; remaining commands were not run.")
            try:
                result = self.file_executor.execute(action)
            except (ActionError, OSError) as exc:
                audit(
                    event="dsl_action_failed",
                    line=command.line_number,
                    command=command.source,
                    action=action,
                    error=str(exc),
                )
                raise DSLExecutionError(command.line_number, str(exc)) from exc
            audit(
                event="dsl_action_completed",
                line=command.line_number,
                command=command.source,
                action=action,
                result=result,
            )
            results.append(result)
        return results

    @staticmethod
    def _parse_command(tokens: list[str], line_number: int) -> dict[str, str]:
        command = tokens[0].upper()
        args = tokens[1:]
        if command == "LIST":
            if len(args) > 1:
                raise DSLParseError(line_number, "LIST accepts zero or one path.")
            return {"action": "list_files", "path": args[0] if args else "."}
        if command == "INFO":
            if len(args) != 1:
                raise DSLParseError(line_number, "Usage: INFO <file>")
            return {"action": "get_info", "path": args[0]}
        if command in {"CREATE_FILE", "CREATE_FOLDER"}:
            if len(args) != 1:
                raise DSLParseError(
                    line_number, f"Usage: {command} <path>"
                )
            return {
                "action": command.casefold(),
                "path": args[0],
            }
        if command in {"RENAME_FILE", "RENAME_FOLDER", "MOVE_FILE"}:
            if len(args) != 3 or args[1].upper() != "TO":
                usage = {
                    "RENAME_FILE": "RENAME_FILE <file> TO <new-name>",
                    "RENAME_FOLDER": "RENAME_FOLDER <folder> TO <new-name>",
                    "MOVE_FILE": "MOVE_FILE <file> TO <destination>",
                }[command]
                raise DSLParseError(line_number, f"Usage: {usage}")
            action_name = {
                "RENAME_FILE": "rename_file",
                "RENAME_FOLDER": "rename_folder",
                "MOVE_FILE": "move_file",
            }[command]
            action = {"action": action_name, "path": args[0]}
            if command == "MOVE_FILE":
                action["destination"] = args[2]
            else:
                action["new_name"] = args[2]
            return action
        if command == "REMOVE_FILE":
            if len(args) != 1:
                raise DSLParseError(line_number, "Usage: REMOVE_FILE <file>")
            return {"action": "remove_file", "path": args[0]}
        raise DSLParseError(line_number, f"Unknown command: {tokens[0]}")
