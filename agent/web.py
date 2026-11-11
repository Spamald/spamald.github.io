"""Render-ready web interface for uploading and safely organizing ZIP files."""

from __future__ import annotations

import html
import json
import os
import secrets
import stat
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from starlette.background import BackgroundTask

from .actions import ActionError, FileActionExecutor
from .audit import AuditLog
from .dsl import DSLCancelled, DSLExecutionError, DSLExecutor, DSLParseError
from .llm import GroqClient, LLMError
from .organizer import (
    ORGANIZATION_DSL_SPEC,
    build_inventory,
    validate_organization_plan,
)

MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_EXTRACTED_BYTES = 250 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 1000
MAX_REVIEW_SESSIONS = 8
SESSION_TTL_SECONDS = 20 * 60


@dataclass
class ReviewSession:
    workspace: tempfile.TemporaryDirectory[str]
    root: Path
    inventory: dict[str, Any]
    created_at: float
    dsl: str | None = None
    stage: str = "inspected"


app = FastAPI(
    title="Safe File Agent",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
audit_log = AuditLog()
_sessions: dict[str, ReviewSession] = {}
_sessions_lock = threading.Lock()


@app.middleware("http")
async def prune_expired_sessions(request, call_next):
    _prune_sessions()
    return await call_next(request)


@app.get("/", response_class=HTMLResponse)
def home() -> HTMLResponse:
    body = """
<h1>Safe File Agent</h1>
<p>Upload a ZIP archive to review its metadata, ask Groq for an organization
plan, confirm the plan, and download the organized archive.</p>
<p>File contents are not read or sent to Groq. The plan is based on filenames,
extensions, and file metadata.</p>
<form action="/inspect" method="post" enctype="multipart/form-data">
  <label>Access token
    <input name="access_token" type="password" autocomplete="current-password" required>
  </label>
  <label>ZIP archive
    <input name="archive" type="file" accept=".zip,application/zip" required>
  </label>
  <button type="submit">Inspect archive</button>
</form>
"""
    return _page("Safe File Agent", body)


@app.post("/inspect", response_class=HTMLResponse)
async def inspect_archive(
    access_token: str = Form(...),
    archive: UploadFile = File(...),
) -> HTMLResponse:
    auth_error = _authorize(access_token)
    if auth_error:
        return auth_error
    if not archive.filename:
        return _page("Invalid archive", "<p>A ZIP filename is required.</p>", 400)
    if Path(archive.filename).suffix.casefold() != ".zip":
        return _page("Invalid archive", "<p>Please upload a .zip archive.</p>", 400)

    workspace = tempfile.TemporaryDirectory(prefix="safe-file-agent-")
    workspace_path = Path(workspace.name)
    archive_path = workspace_path / "upload.zip"
    extracted_root = workspace_path / "files"
    extracted_root.mkdir()

    try:
        size = await _save_upload(archive, archive_path)
        if size == 0 or not zipfile.is_zipfile(archive_path):
            raise ValueError("The uploaded file is not a valid ZIP archive.")
        _extract_archive(archive_path, extracted_root)
        archive_path.unlink()
        file_manager = FileActionExecutor(extracted_root)
        inventory = build_inventory(file_manager)
        public_inventory = _public_inventory(inventory, Path(archive.filename).name)
        session_id = _store_session(
            ReviewSession(workspace, extracted_root, inventory, time.monotonic())
        )
    except (OSError, ValueError, zipfile.BadZipFile, ActionError) as exc:
        workspace.cleanup()
        return _page("Archive rejected", f"<p>{html.escape(str(exc))}</p>", 400)
    finally:
        await archive.close()

    try:
        audit_log.record(
            event="web_archive_inspected",
            archive_name=Path(archive.filename).name,
            upload_size_bytes=size,
            root=str(extracted_root),
            file_count=len(inventory["files"]),
            folder_count=len(inventory["folders"]),
        )
    except RuntimeError as exc:
        _remove_session(session_id)
        return _page("Audit log error", f"<p>{html.escape(str(exc))}</p>", 500)

    body = f"""
<h1>Archive inventory</h1>
<p>Archive: <strong>{html.escape(Path(archive.filename).name)}</strong></p>
<p>{len(inventory["files"])} file(s), {len(inventory["folders"])} folder(s).</p>
<p>Review the metadata below. File contents were not read. Clicking the button
explicitly sends this metadata to Groq to generate an organization plan.</p>
<pre>{html.escape(_pretty_json(public_inventory))}</pre>
<form action="/generate" method="post">
  <input type="hidden" name="access_token" value="{html.escape(access_token, quote=True)}">
  <input type="hidden" name="session_id" value="{html.escape(session_id, quote=True)}">
  <button type="submit">Send metadata to Groq and generate plan</button>
</form>
<p><a href="/">Start over</a></p>
"""
    return _page("Archive inventory", body)


@app.post("/generate", response_class=HTMLResponse)
def generate_plan(
    access_token: str = Form(...),
    session_id: str = Form(...),
) -> HTMLResponse:
    auth_error = _authorize(access_token)
    if auth_error:
        return auth_error
    session = _get_session(session_id)
    if session is None:
        return _page("Review expired", "<p>This upload review expired. Please upload the ZIP again.</p>", 410)
    if not _transition_session(session_id, session, "inspected", "generating"):
        return _page("Plan already requested", "<p>This upload is already being processed or has a plan.</p>", 409)

    file_manager = FileActionExecutor(session.root)
    try:
        client = GroqClient()
        dsl = client.generate_organization_dsl(
            _public_inventory(session.inventory, "uploaded archive"),
            ORGANIZATION_DSL_SPEC,
        )
        commands = validate_organization_plan(dsl, file_manager, session.inventory)
    except (LLMError, DSLParseError, ActionError) as exc:
        _transition_session(session_id, session, "generating", "inspected")
        try:
            _audit_error("web_organization_plan_failed", session, str(exc))
        except RuntimeError as audit_exc:
            _remove_session(session_id)
            return _page("Audit log error", f"<p>{html.escape(str(audit_exc))}</p>", 500)
        return _page("Plan rejected", f"<p>{html.escape(str(exc))}</p><p><a href=\"/\">Start over</a></p>", 422)

    session.dsl = dsl
    if not _transition_session(session_id, session, "generating", "reviewed"):
        return _page("Review expired", "<p>This upload review expired. Please upload the ZIP again.</p>", 410)
    try:
        audit_log.record(
            event="web_organization_plan_generated",
            root=str(session.root),
            command_count=len(commands),
            dsl=dsl,
        )
    except RuntimeError as exc:
        _remove_session(session_id)
        return _page("Audit log error", f"<p>{html.escape(str(exc))}</p>", 500)

    body = f"""
<h1>Review organization plan</h1>
<p>Groq proposed these DSL commands. Review them before continuing.
Each operation is confined to this uploaded archive. The original files on
your computer are not modified.</p>
<pre>{html.escape(dsl)}</pre>
<form action="/execute" method="post">
  <input type="hidden" name="access_token" value="{html.escape(access_token, quote=True)}">
  <input type="hidden" name="session_id" value="{html.escape(session_id, quote=True)}">
  <label><input type="checkbox" name="confirm_plan" value="yes" required>
    I reviewed this plan and authorize these file changes.</label>
  <button type="submit">Apply plan and download organized ZIP</button>
</form>
<p><a href="/">Cancel</a></p>
"""
    return _page("Review organization plan", body)


@app.post("/execute")
def execute_plan(
    access_token: str = Form(...),
    session_id: str = Form(...),
    confirm_plan: str = Form(""),
) -> Response:
    auth_error = _authorize(access_token)
    if auth_error:
        return auth_error
    session = _get_session(session_id)
    if session is None or session.dsl is None:
        return _page("Review expired", "<p>This plan expired. Please upload the ZIP again.</p>", 410)
    if confirm_plan != "yes":
        return _page("Confirmation required", "<p>Confirm the reviewed plan before applying it.</p>", 400)
    if not _transition_session(session_id, session, "reviewed", "executing"):
        return _page("Plan already submitted", "<p>This plan is already being applied or has expired.</p>", 409)

    file_manager = FileActionExecutor(session.root)
    try:
        audit_log.record(
            event="web_organization_execution_started",
            root=str(session.root),
            command_count=len(DSLExecutor(file_manager).parse(session.dsl)),
        )

        def audit_action(**fields: Any) -> None:
            audit_log.record(root=str(session.root), **fields)

        DSLExecutor(file_manager).execute(
            session.dsl,
            confirm=lambda _action: True,
            audit=audit_action,
        )
        output_path = Path(session.workspace.name) / "organized-files.zip"
        _create_output_archive(session.root, output_path)
        audit_log.record(
            event="web_organization_completed",
            root=str(session.root),
        )
    except (OSError, RuntimeError, DSLExecutionError, DSLParseError, ActionError) as exc:
        try:
            _audit_error("web_organization_execution_failed", session, str(exc))
        except RuntimeError as audit_exc:
            return _page("Audit log error", f"<p>{html.escape(str(audit_exc))}</p>", 500)
        return _page("Organization failed", f"<p>{html.escape(str(exc))}</p><p>The uploaded workspace is temporary; please start over if needed.</p>", 500)

    return FileResponse(
        output_path,
        media_type="application/zip",
        filename="organized-files.zip",
        background=BackgroundTask(_remove_session, session_id),
        headers={
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _authorize(token: str) -> HTMLResponse | None:
    expected = os.getenv("APP_ACCESS_TOKEN")
    if not expected:
        return _page(
            "Service not configured",
            "<p>Set APP_ACCESS_TOKEN in the service environment before using the app.</p>",
            503,
        )
    if not secrets.compare_digest(token, expected):
        return _page("Unauthorized", "<p>Invalid access token.</p>", 401)
    return None


async def _save_upload(upload: UploadFile, destination: Path) -> int:
    total = 0
    with destination.open("xb") as output:
        while chunk := await upload.read(1024 * 1024):
            total += len(chunk)
            if total > MAX_UPLOAD_BYTES:
                raise ValueError(
                    f"ZIP exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB upload limit."
                )
            output.write(chunk)
    return total


def _extract_archive(archive_path: Path, destination: Path) -> None:
    extracted_bytes = 0
    seen_paths: dict[str, tuple[str, str]] = {}
    with zipfile.ZipFile(archive_path) as archive:
        entries = archive.infolist()
        if len(entries) > MAX_ARCHIVE_ENTRIES:
            raise ValueError(f"ZIP contains more than {MAX_ARCHIVE_ENTRIES} entries.")
        if sum(item.file_size for item in entries) > MAX_EXTRACTED_BYTES:
            raise ValueError("ZIP exceeds the maximum uncompressed size.")

        for info in entries:
            relative = _safe_archive_path(info.filename)
            mode = info.external_attr >> 16
            kind = stat.S_IFMT(mode)
            if kind == stat.S_IFLNK:
                raise ValueError(f"ZIP symbolic links are not allowed: {info.filename}")
            if kind not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise ValueError(f"Unsupported special file in ZIP: {info.filename}")
            if (kind == stat.S_IFDIR) != info.is_dir() and kind != 0:
                raise ValueError(f"Invalid file type in ZIP: {info.filename}")
            _register_archive_path(relative, info.is_dir(), seen_paths)

            target = destination.joinpath(*relative.parts)
            if not target.resolve().is_relative_to(destination.resolve()):
                raise ValueError("ZIP entry escapes the extraction folder.")
            if info.is_dir():
                if target.exists() and not target.is_dir():
                    raise ValueError(f"Conflicting ZIP entry: {info.filename}")
                target.mkdir(parents=True, exist_ok=True)
                continue
            if target.exists() or target.is_symlink():
                raise ValueError(f"Duplicate or conflicting ZIP entry: {info.filename}")
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                with archive.open(info) as source, target.open("xb") as output:
                    while chunk := source.read(1024 * 1024):
                        extracted_bytes += len(chunk)
                        if extracted_bytes > MAX_EXTRACTED_BYTES:
                            raise ValueError("ZIP exceeds the maximum uncompressed size.")
                        output.write(chunk)
            except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                target.unlink(missing_ok=True)
                raise ValueError(f"Could not safely extract {info.filename}: {exc}") from exc
            try:
                modified_time = time.mktime((*info.date_time, 0, 0, -1))
                os.utime(target, (modified_time, modified_time))
            except (OverflowError, OSError, ValueError):
                pass


def _safe_archive_path(name: str) -> PurePosixPath:
    if not name or "\\" in name or "\x00" in name:
        raise ValueError(f"Invalid path in ZIP: {name!r}")
    raw_parts = name.rstrip("/").split("/")
    if any(part in ("", ".", "..") for part in raw_parts):
        raise ValueError(f"Unsafe path in ZIP: {name}")
    path = PurePosixPath(name)
    windows_path = PureWindowsPath(name)
    if path.is_absolute() or windows_path.is_absolute() or windows_path.drive:
        raise ValueError(f"Absolute path in ZIP is not allowed: {name}")
    if any(part in ("", ".", "..") for part in path.parts):
        raise ValueError(f"Unsafe path in ZIP: {name}")
    invalid_windows_chars = set('<>:"|?*')
    reserved_names = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
    for part in path.parts:
        if (
            any(ord(char) < 32 or char in invalid_windows_chars for char in part)
            or part.endswith((".", " "))
            or part.split(".", 1)[0].upper() in reserved_names
        ):
            raise ValueError(f"Unsupported filename in ZIP: {name}")
    return path


def _register_archive_path(
    path: PurePosixPath,
    is_directory: bool,
    seen_paths: dict[str, tuple[str, str]],
) -> None:
    parts = path.parts
    for index in range(len(parts)):
        spelling = PurePosixPath(*parts[: index + 1]).as_posix()
        is_leaf = index == len(parts) - 1
        kind = "file" if is_leaf and not is_directory else "directory"
        key = spelling.casefold()
        previous = seen_paths.get(key)
        if previous is not None:
            previous_kind, previous_spelling = previous
            if previous_spelling != spelling:
                raise ValueError(f"Case-insensitive ZIP path collision: {spelling}")
            if previous_kind == "file" or kind == "file":
                raise ValueError(f"Duplicate or conflicting ZIP entry: {spelling}")
        else:
            seen_paths[key] = (kind, spelling)


def _public_inventory(
    inventory: dict[str, Any], archive_name: str
) -> dict[str, Any]:
    return {
        "managed_root": archive_name,
        "folders": [
            {key: value for key, value in folder.items() if key != "full_path"}
            for folder in inventory["folders"]
        ],
        "files": [
            {key: value for key, value in file.items() if key != "full_path"}
            for file in inventory["files"]
        ],
        "skipped_symlinks": inventory["skipped_symlinks"],
    }


def _create_output_archive(root: Path, destination: Path) -> None:
    with zipfile.ZipFile(
        destination, mode="w", compression=zipfile.ZIP_DEFLATED
    ) as archive:
        paths = sorted(root.rglob("*"), key=lambda item: item.as_posix().casefold())
        for path in paths:
            if path.is_symlink():
                raise OSError(f"Symbolic link found in output workspace: {path.name}")
            if path.is_dir():
                archive.writestr(path.relative_to(root).as_posix() + "/", b"")
            elif path.is_file():
                archive.write(path, path.relative_to(root).as_posix())


def _store_session(session: ReviewSession) -> str:
    _prune_sessions()
    with _sessions_lock:
        if len(_sessions) >= MAX_REVIEW_SESSIONS:
            session.workspace.cleanup()
            raise ValueError("The server is busy with other uploads. Please try again later.")
        session_id = secrets.token_urlsafe(32)
        _sessions[session_id] = session
        return session_id


def _get_session(session_id: str) -> ReviewSession | None:
    _prune_sessions()
    with _sessions_lock:
        return _sessions.get(session_id)


def _transition_session(
    session_id: str, expected_session: ReviewSession, old_stage: str, new_stage: str
) -> bool:
    with _sessions_lock:
        session = _sessions.get(session_id)
        if session is not expected_session or session.stage != old_stage:
            return False
        session.stage = new_stage
        return True


def _prune_sessions() -> None:
    now = time.monotonic()
    with _sessions_lock:
        expired = [
            session_id
            for session_id, session in _sessions.items()
            if now - session.created_at > SESSION_TTL_SECONDS
        ]
        sessions = [_sessions.pop(session_id) for session_id in expired]
    for session in sessions:
        session.workspace.cleanup()


def _remove_session(session_id: str) -> None:
    with _sessions_lock:
        session = _sessions.pop(session_id, None)
    if session:
        session.workspace.cleanup()


def _audit_error(event: str, session: ReviewSession, message: str) -> None:
    try:
        audit_log.record(event=event, root=str(session.root), error=message)
    except RuntimeError as exc:
        raise RuntimeError(f"Could not write audit log: {exc}") from exc


def _pretty_json(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False)


def _page(title: str, body: str, status_code: int = 200) -> HTMLResponse:
    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    body {{ font: 16px system-ui, sans-serif; line-height: 1.5; margin: 2rem auto; max-width: 900px; padding: 0 1rem; color: #172033; }}
    form {{ display: grid; gap: 1rem; margin: 1.5rem 0; }}
    label {{ display: grid; gap: .4rem; }}
    input, button {{ font: inherit; padding: .6rem; }}
    button {{ cursor: pointer; width: fit-content; }}
    pre {{ background: #f1f4f9; border-radius: .4rem; overflow: auto; padding: 1rem; white-space: pre-wrap; overflow-wrap: anywhere; }}
  </style>
</head>
<body>{body}</body>
</html>"""
    return HTMLResponse(
        document,
        status_code=status_code,
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": (
                "default-src 'none'; style-src 'unsafe-inline'; "
                "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
            ),
            "Cache-Control": "no-store",
        },
    )
