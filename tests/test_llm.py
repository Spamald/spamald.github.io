import json
import unittest
from io import BytesIO
from unittest.mock import patch
from urllib.error import HTTPError

from agent.llm import GroqClient, LLMError


class GroqClientTests(unittest.TestCase):
    def test_requires_api_key(self):
        with patch.dict("os.environ", {}, clear=True), self.assertRaises(LLMError):
            GroqClient(api_key="")

    @patch("agent.llm.urlopen")
    def test_sends_request_and_parses_action(self, mock_urlopen):
        response_body = {
            "choices": [
                {"message": {"content": '{"action":"list_files","path":"."}'}}
            ]
        }
        mock_urlopen.return_value = BytesIO(json.dumps(response_body).encode())
        client = GroqClient(api_key="test-key")

        result = client.plan("List this folder")

        self.assertEqual(result, {"action": "list_files", "path": "."})
        request = mock_urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.groq.com/openai/v1/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-key")
        self.assertEqual(request.get_header("User-agent"), "safe-file-agent/0.1.0")
        request_payload = json.loads(request.data)
        self.assertEqual(request_payload["model"], "openai/gpt-oss-20b")
        self.assertEqual(request_payload["response_format"], {"type": "json_object"})

    def test_model_can_be_overridden_by_environment(self):
        with patch.dict("os.environ", {"GROQ_MODEL": "custom/model"}):
            client = GroqClient(api_key="test-key")
        self.assertEqual(client.model, "custom/model")

    @patch("agent.llm.urlopen")
    def test_generates_dsl_from_metadata_without_json_mode(self, mock_urlopen):
        response_body = {
            "choices": [{"message": {"content": 'CREATE_FOLDER "text"'}}]
        }
        mock_urlopen.return_value = BytesIO(json.dumps(response_body).encode())
        client = GroqClient(api_key="test-key")
        inventory = {
            "managed_root": "C:/files",
            "files": [{"path": "notes.txt", "extension": ".txt"}],
        }

        result = client.generate_organization_dsl(inventory, "DSL SPEC")

        self.assertEqual(result, 'CREATE_FOLDER "text"')
        request = mock_urlopen.call_args.args[0]
        payload = json.loads(request.data)
        self.assertNotIn("response_format", payload)
        self.assertIn("DSL SPEC", payload["messages"][1]["content"])
        self.assertIn("notes.txt", payload["messages"][1]["content"])

    @patch("agent.llm.time.sleep")
    @patch("agent.llm.urlopen")
    def test_retries_rate_limit_then_succeeds(self, mock_urlopen, mock_sleep):
        limited = HTTPError(
            "https://api.groq.com",
            429,
            "Too Many Requests",
            {"Retry-After": "1"},
            BytesIO(b"rate limited"),
        )
        response_body = {
            "choices": [
                {"message": {"content": '{"action":"get_info","path":"a.txt"}'}}
            ]
        }
        mock_urlopen.side_effect = [limited, BytesIO(json.dumps(response_body).encode())]

        result = GroqClient(api_key="test-key", max_attempts=2).plan("Info on a.txt")

        self.assertEqual(result["action"], "get_info")
        mock_sleep.assert_called_once_with(1.0)

    @patch("agent.llm.urlopen")
    def test_reports_non_retryable_api_error(self, mock_urlopen):
        mock_urlopen.side_effect = HTTPError(
            "https://api.groq.com", 401, "Unauthorized", {}, BytesIO(b"bad key")
        )
        with self.assertRaisesRegex(LLMError, "HTTP 401"):
            GroqClient(api_key="test-key").plan("list files")

    @patch("agent.llm.urlopen")
    def test_includes_cloudflare_request_id_in_error(self, mock_urlopen):
        mock_urlopen.side_effect = HTTPError(
            "https://api.groq.com",
            403,
            "Forbidden",
            {"CF-Ray": "example-ray-id"},
            BytesIO(b"error code: 1010"),
        )
        with self.assertRaisesRegex(LLMError, "example-ray-id.*1010"):
            GroqClient(api_key="test-key").plan("list files")


if __name__ == "__main__":
    unittest.main()
