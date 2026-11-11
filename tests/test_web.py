import io
import html
import os
import re
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from agent.audit import AuditLog
from agent import web


class StubWebClient:
    def __init__(self):
        self.inventory = None
        self.calls = 0

    def generate_organization_dsl(self, inventory, _specification):
        self.calls += 1
        self.inventory = inventory
        files = inventory["files"]
        lines = ['CREATE_FOLDER "organized"']
        lines.extend(
            f'MOVE_FILE "{item["path"]}" TO "organized"'
            for item in files
        )
        return "\n".join(lines)


def make_zip(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return buffer.getvalue()


class WebApplicationTests(unittest.TestCase):
    def test_upload_review_generate_confirm_and_download(self):
        archive_bytes = make_zip(
            [("bundle/notes.txt", "private content not sent to Groq")]
        )
        client = StubWebClient()
        with tempfile.TemporaryDirectory() as temp_dir:
            audit_path = Path(temp_dir) / "audit.jsonl"
            with (
                patch.dict(os.environ, {"APP_ACCESS_TOKEN": "test-token"}),
                patch.object(web, "audit_log", AuditLog(audit_path)),
                patch.object(web, "GroqClient", return_value=client),
                TestClient(web.app) as http,
            ):
                inspect = http.post(
                    "/inspect",
                    data={"access_token": "test-token"},
                    files={
                        "archive": (
                            "my-files.zip",
                            archive_bytes,
                            "application/zip",
                        )
                    },
                )
                self.assertEqual(inspect.status_code, 200)
                self.assertIn("bundle/notes.txt", inspect.text)
                self.assertNotIn("private content not sent to Groq", inspect.text)
                session_id = re.search(
                    r'name="session_id" value="([^"]+)"', inspect.text
                ).group(1)

                generated = http.post(
                    "/generate",
                    data={
                        "access_token": "test-token",
                        "session_id": session_id,
                    },
                )
                self.assertEqual(generated.status_code, 200)
                self.assertIn(
                    'MOVE_FILE "bundle/notes.txt" TO "organized"',
                    html.unescape(generated.text),
                )
                self.assertNotIn("private content not sent to Groq", str(client.inventory))
                duplicate_generation = http.post(
                    "/generate",
                    data={
                        "access_token": "test-token",
                        "session_id": session_id,
                    },
                )
                self.assertEqual(duplicate_generation.status_code, 409)
                self.assertEqual(client.calls, 1)

                unconfirmed = http.post(
                    "/execute",
                    data={
                        "access_token": "test-token",
                        "session_id": session_id,
                    },
                )
                self.assertEqual(unconfirmed.status_code, 400)

                downloaded = http.post(
                    "/execute",
                    data={
                        "access_token": "test-token",
                        "session_id": session_id,
                        "confirm_plan": "yes",
                    },
                )
                self.assertEqual(downloaded.status_code, 200)
                self.assertEqual(downloaded.headers["content-type"], "application/zip")
                with zipfile.ZipFile(io.BytesIO(downloaded.content)) as result:
                    self.assertEqual(
                        set(result.namelist()),
                        {
                            "bundle/",
                            "organized/",
                            "organized/notes.txt",
                        },
                    )
                    self.assertEqual(
                        result.read("organized/notes.txt"),
                        b"private content not sent to Groq",
                    )
                repeated_execution = http.post(
                    "/execute",
                    data={
                        "access_token": "test-token",
                        "session_id": session_id,
                        "confirm_plan": "yes",
                    },
                )
                self.assertEqual(repeated_execution.status_code, 410)

                events = [
                    line
                    for line in audit_path.read_text(encoding="utf-8").splitlines()
                ]
                self.assertTrue(any("web_organization_completed" in line for line in events))

    def test_rejects_bad_token_and_invalid_zip(self):
        with patch.dict(os.environ, {"APP_ACCESS_TOKEN": "test-token"}):
            with TestClient(web.app) as http:
                unauthorized = http.post(
                    "/inspect",
                    data={"access_token": "wrong"},
                    files={"archive": ("files.zip", make_zip([]), "application/zip")},
                )
                self.assertEqual(unauthorized.status_code, 401)

                invalid = http.post(
                    "/inspect",
                    data={"access_token": "test-token"},
                    files={"archive": ("files.zip", b"not a zip", "application/zip")},
                )
                self.assertEqual(invalid.status_code, 400)
                self.assertIn("not a valid ZIP", invalid.text)

    def test_rejects_path_traversal_symlink_and_case_collision(self):
        malicious_archives = []
        malicious_archives.append(make_zip([("../escape.txt", "bad")]))
        symlink_buffer = io.BytesIO()
        with zipfile.ZipFile(symlink_buffer, "w") as archive:
            info = zipfile.ZipInfo("link")
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, "target")
        malicious_archives.append(symlink_buffer.getvalue())
        malicious_archives.append(
            make_zip([("Folder/file.txt", "1"), ("folder/other.txt", "2")])
        )
        malicious_archives.append(
            make_zip([("duplicate.txt", "1"), ("duplicate.txt", "2")])
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            for index, content in enumerate(malicious_archives):
                archive_path = Path(temp_dir) / f"bad-{index}.zip"
                output_path = Path(temp_dir) / f"out-{index}"
                output_path.mkdir()
                archive_path.write_bytes(content)
                with self.subTest(archive=index), self.assertRaises(ValueError):
                    web._extract_archive(archive_path, output_path)
            self.assertFalse((Path(temp_dir) / "escape.txt").exists())

    def test_health_check_is_available(self):
        with TestClient(web.app) as http:
            response = http.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_requires_configured_access_token(self):
        with patch.dict(os.environ, {}, clear=True):
            with TestClient(web.app) as http:
                response = http.post(
                    "/inspect",
                    data={"access_token": "anything"},
                    files={"archive": ("files.zip", make_zip([]), "application/zip")},
                )
        self.assertEqual(response.status_code, 503)


if __name__ == "__main__":
    unittest.main()
