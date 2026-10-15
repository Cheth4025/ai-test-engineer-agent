"""HTTP client for the Decider 2B server (`strands-decider serve`).

One attempt, no retries. Never returns a made-up answer: any failure to reach
the server or parse its response raises DeciderError.
"""

from __future__ import annotations

import time

import httpx


class DeciderError(Exception):
    """Raised when the Decider server cannot be reached or its response is unusable."""


class DeciderClient:
    """Thin HTTP wrapper around POST {url}/v1/systemone."""

    def __init__(
        self,
        url: str,
        timeout_s: float,
        connect_timeout_s: float = 5.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        self.timeout_s = timeout_s
        self.connect_timeout_s = connect_timeout_s
        self._transport = transport

    def ask(self, state: str | dict, questions: dict) -> dict:
        """POST one request with every named question against one state.

        Returns {"model", "usage", "answers", "latency_ms", "raw_response"}.
        latency_ms is always self-measured wall-clock time. Raises DeciderError
        on timeout, connection error, non-200 status, or a response missing an
        expected answer -- never returns a substitute/fabricated answer.
        """
        timeout = httpx.Timeout(
            connect=self.connect_timeout_s,
            read=self.timeout_s,
            write=self.timeout_s,
            pool=self.timeout_s,
        )
        payload = {"state": state, "questions": questions}

        start = time.monotonic()
        try:
            with httpx.Client(transport=self._transport, timeout=timeout) as client:
                response = client.post(f"{self.url}/v1/systemone", json=payload)
        except httpx.TimeoutException as exc:
            raise DeciderError(f"Decider request timed out: {exc}") from exc
        except httpx.TransportError as exc:
            raise DeciderError(f"Decider connection error: {exc}") from exc
        latency_ms = int((time.monotonic() - start) * 1000)

        if response.status_code != 200:
            raise DeciderError(
                f"Decider returned HTTP {response.status_code}: {response.text[:500]}"
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise DeciderError(f"Decider response was not valid JSON: {exc}") from exc

        answers = body.get("answers")
        if not isinstance(answers, dict):
            raise DeciderError(f"Decider response missing 'answers': {body!r}")
        for name in questions:
            if name not in answers:
                raise DeciderError(f"Decider response missing answer for question {name!r}")

        return {
            "model": body.get("model"),
            "usage": body.get("usage"),
            "answers": answers,
            "latency_ms": latency_ms,
            "raw_response": body,
        }
