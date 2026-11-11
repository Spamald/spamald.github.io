"""Interactive terminal interface for the safe file agent."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .actions import ActionError, FileActionExecutor
from .audit import AuditLog
from .dsl import DSLCancelled, DSLExecutionError, DSLExecutor, DSLParseError
from .llm import GroqClient, LLMError
from .organizer import (
    ORGANIZATION_DSL_SPEC,
    build_inventory,
    validate_organization_plan,
)


def _confirm(action: dict[str, Any]) -> bool:
    print("This operation will change the filesystem:")
    print(json.dumps(action, indent=2, ensure_ascii=False))
    try:
        answer = input("Type 'yes' to confirm: ").strip().casefold()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer == "yes"


def _record(audit: AuditLog, **fields: Any) -> bool:
    try:
        audit.record(**fields)
    except RuntimeError as exc:
        print(f"Fatal audit-log error: {exc}", file=sys.stderr)
        return False
    return True


def run(root: Path, client: GroqClient, audit: AuditLog) -> int:
    try:
        executor = FileActionExecutor(root)
    except ActionError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    print(f"File agent ready. Managed root: {executor.root}")
    print("Enter a request, or type 'exit' to quit.")
    while True:
        try:
            user_request = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if user_request.casefold() in {"exit", "quit"}:
            return 0
        if not user_request:
            continue

        if not _record(audit, event="request", request=user_request):
            return 2
        try:
            proposed = client.plan(user_request)
            validated = executor.validate(proposed)
            if not _record(
                audit, event="proposed_action", request=user_request, action=validated
            ):
                return 2
            print("Proposed action:")
            print(json.dumps(validated, indent=2, ensure_ascii=False))
            if executor.requires_confirmation(validated) and not _confirm(validated):
                if not _record(
                    audit,
                    event="action_cancelled",
                    request=user_request,
                    action=validated,
                    result="User declined confirmation.",
                ):
                    return 2
                print("Cancelled; no changes were made.")
                continue
            result = executor.execute(validated)
            if not _record(
                audit,
                event="action_completed",
                request=user_request,
                action=validated,
                result=result,
            ):
                return 2
            print("Result:")
            print(json.dumps(result, indent=2, ensure_ascii=False))
        except (ActionError, LLMError, OSError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            if not _record(
                audit, event="action_failed", request=user_request, error=str(exc)
            ):
                return 2


def run_dsl(root: Path, script_path: Path, audit: AuditLog) -> int:
    try:
        executor = FileActionExecutor(root)
        source = script_path.read_text(encoding="utf-8")
    except (ActionError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    if not _record(audit, event="dsl_program_started", file=str(script_path), source=source):
        return 2
    print(f"Running DSL file {script_path} against {executor.root}")

    def record_event(**fields: Any) -> None:
        if not _record(audit, file=str(script_path), **fields):
            raise RuntimeError("Could not write DSL action to the audit log.")
        if fields.get("event") == "dsl_action_completed":
            print(f"Line {fields['line']} result:")
            print(json.dumps(fields["result"], indent=2, ensure_ascii=False))

    def confirm_action(action: dict[str, Any]) -> bool:
        return _confirm(action)

    try:
        DSLExecutor(executor).execute(source, confirm_action, record_event)
    except DSLCancelled as exc:
        print(exc)
        return 0
    except (DSLParseError, DSLExecutionError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        _record(audit, event="dsl_program_failed", file=str(script_path), error=str(exc))
        return 2

    if not _record(audit, event="dsl_program_completed", file=str(script_path)):
        return 2
    return 0


def run_organizer(client: GroqClient, audit: AuditLog) -> int:
    try:
        raw_root = input("Base folder to organize (path): ").strip().strip('"')
    except (EOFError, KeyboardInterrupt):
        print()
        return 0
    if not raw_root:
        print("Error: a base folder path is required.", file=sys.stderr)
        return 2
    root = Path(raw_root).expanduser()
    if not root.is_absolute():
        root = Path.cwd() / root

    try:
        file_manager = FileActionExecutor(root)
        inventory = build_inventory(file_manager)
    except (ActionError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    print(f"\nFile inventory for {file_manager.root}:")
    print(json.dumps(inventory, indent=2, ensure_ascii=False))
    print(
        "\nOnly metadata (paths, names, extensions, sizes, and modified times) "
        "will be sent to Groq. File contents are not read or sent."
    )
    try:
        answer = input("Send this metadata to Groq and generate an organization plan? [yes/no] ")
    except (EOFError, KeyboardInterrupt):
        print()
        return 0
    if answer.strip().casefold() != "yes":
        print("Cancelled; no metadata was sent and no files were changed.")
        return 0

    if not _record(
        audit,
        event="organize_inventory_sent",
        root=str(file_manager.root),
        file_count=len(inventory["files"]),
        folder_count=len(inventory["folders"]),
        symlink_count=len(inventory["skipped_symlinks"]),
    ):
        return 2

    try:
        generated = client.generate_organization_dsl(inventory, ORGANIZATION_DSL_SPEC)
        commands = validate_organization_plan(generated, file_manager, inventory)
    except (LLMError, DSLParseError, ActionError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        _record(audit, event="organize_plan_failed", root=str(file_manager.root), error=str(exc))
        return 2

    print("\nProposed DSL plan:")
    print(generated)
    if not _record(
        audit,
        event="organize_plan_generated",
        root=str(file_manager.root),
        command_count=len(commands),
        dsl=generated,
    ):
        return 2
    if not commands:
        print("No files need organizing.")
        return 0

    def record_event(**fields: Any) -> None:
        if not _record(audit, root=str(file_manager.root), **fields):
            raise RuntimeError("Could not write organization action to the audit log.")

    try:
        DSLExecutor(file_manager).execute(generated, _confirm, record_event)
    except DSLCancelled as exc:
        print(exc)
        return 0
    except (DSLParseError, DSLExecutionError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        _record(
            audit,
            event="organize_execution_failed",
            root=str(file_manager.root),
            error=str(exc),
        )
        return 2
    if not _record(
        audit, event="organize_completed", root=str(file_manager.root)
    ):
        return 2
    print("Organization completed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="A safe, LLM-powered file-management agent.")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="Directory the agent is allowed to manage (default: current directory).",
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--dsl",
        type=Path,
        help="Execute a file-manager DSL script; no LLM API key is needed.",
    )
    modes.add_argument(
        "--organize",
        action="store_true",
        help="Inventory a prompted folder, generate an organization plan with Groq, and run it.",
    )
    args = parser.parse_args()
    audit = AuditLog()
    if args.dsl:
        return run_dsl(args.root, args.dsl, audit)
    try:
        client = GroqClient()
    except LLMError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    if args.organize:
        return run_organizer(client, audit)
    return run(args.root, client, audit)


if __name__ == "__main__":
    raise SystemExit(main())
