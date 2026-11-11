"""Groq chat-completions client using only the Python standard library."""

from __future__ import annotations

import json
import os
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class LLMError(RuntimeError):
    """Raised when an LLM request fails or returns an invalid response."""


SYSTEM_PROMPT = """You translate a user's file-management request into exactly
one JSON object. Do not include markdown or commentary. Never invent file
contents or claim an action was performed.

Allowed actions and required arguments:
{"action":"list_files","path":"relative directory path"}
{"action":"get_info","path":"relative file path"}
{"action":"create_file","path":"relative file path"}
{"action":"create_folder","path":"relative folder path"}
{"action":"rename_file","path":"relative file path","new_name":"new basename"}
{"action":"rename_folder","path":"relative folder path","new_name":"new basename"}
{"action":"move_file","path":"relative source file path","destination":"relative destination path"}
{"action":"remove_file","path":"relative file path"}

Paths must be relative to the managed root. For requests to list files "in
the directory" without naming a subdirectory, use ".". The managed root itself
is already selected, so do not prefix paths with its directory name. If a
user names a file without its extension, use that exact name as the path; the
agent resolves it only when exactly one matching file exists. If the request
is ambiguous or not supported, return
{"action":"unsupported","reason":"brief explanation"}.
Choose only one action. Do not return plans, multiple actions, or extra keys."""


class GroqClient:
    """Small client for Groq's OpenAI-compatible chat completions endpoint."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        api_base: str | None = None,
        timeout: float = 30,
        max_attempts: int = 3,
    ) -> None:
        self.api_key = api_key or os.getenv("GROQ_API_KEY")
        self.model = model or os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
        self.api_base = (
            api_base or os.getenv("GROQ_API_BASE", "https://api.groq.com/openai/v1")
        ).rstrip("/")
        self.timeout = timeout
        self.max_attempts = max_attempts
        if not self.api_key:
            raise LLMError(
                "GROQ_API_KEY is not set. Set it in your environment before starting."
            )

    def plan(self, user_request: str) -> dict[str, object]:
        content = self._complete(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_request},
            ],
            json_mode=True,
        )
        try:
            action = json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMError(f"Groq returned invalid action JSON: {exc}") from exc
        if not isinstance(action, dict):
            raise LLMError("The LLM response must be a JSON object.")
        return action

    def generate_organization_dsl(
        self, inventory: dict[str, Any], dsl_specification: str
    ) -> str:
        content = self._complete(
            [
                {
                    "role": "system",
                    "content": (
                        "Generate a file organization plan. Follow the supplied "
                        "DSL specification exactly. Return only DSL commands."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"{dsl_specification}\n\n"
                        "Organize every inventoried file into folders. "
                        "Use the metadata only; no file contents were provided. "
                        "Create sensible groups by extension and filename. "
                        "Create each folder before using it as a destination.\n\n"
                        f"Inventory JSON:\n{json.dumps(inventory, ensure_ascii=False)}"
                    ),
                },
            ],
            json_mode=False,
        ).strip()
        if content.startswith("```") and content.endswith("```"):
            lines = content.splitlines()
            content = "\n".join(lines[1:-1]).strip()
        if not content:
            raise LLMError("Groq returned an empty organization plan.")
        return content

    def _complete(self, messages: list[dict[str, str]], *, json_mode: bool) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "temperature": 0,
            "messages": messages,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        request = Request(
            f"{self.api_base}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "safe-file-agent/0.1.0",
            },
            method="POST",
        )

        for attempt in range(self.max_attempts):
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    body = json.loads(response.read().decode("utf-8"))
                content = body["choices"][0]["message"]["content"]
                if not isinstance(content, str):
                    raise LLMError("Groq returned a non-text response.")
                return content
            except HTTPError as exc:
                if exc.code == 429 or 500 <= exc.code < 600:
                    if attempt + 1 < self.max_attempts:
                        self._wait_before_retry(attempt, exc.headers.get("Retry-After"))
                        continue
                details = exc.read().decode("utf-8", errors="replace")
                ray_id = exc.headers.get("CF-Ray")
                request_id = exc.headers.get("x-request-id")
                diagnostic_id = ray_id or request_id
                diagnostic = f" (request ID: {diagnostic_id})" if diagnostic_id else ""
                raise LLMError(
                    f"Groq API returned HTTP {exc.code}{diagnostic}: {details}"
                ) from exc
            except URLError as exc:
                if attempt + 1 < self.max_attempts:
                    self._wait_before_retry(attempt, None)
                    continue
                raise LLMError(f"Could not reach Groq API: {exc.reason}") from exc
            except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
                raise LLMError(f"Groq returned an invalid response: {exc}") from exc

        raise LLMError("Groq request failed after retrying.")

    @staticmethod
    def _wait_before_retry(attempt: int, retry_after: str | None) -> None:
        try:
            delay = min(max(float(retry_after), 1), 30) if retry_after else 2**attempt
        except ValueError:
            delay = 2**attempt
        time.sleep(delay)
