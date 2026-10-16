"""Real-Decider-server smoke test. Deselected by default (addopts = -m "not live").

Run manually once a Decider server is up:
    .venv/Scripts/python.exe -m pytest tests/live -v -m live
"""

from __future__ import annotations

import os

import pytest

from decider_gate.client import DeciderClient
from decider_gate.gate import ConfidenceGate

pytestmark = pytest.mark.live


def test_live_choice_and_noul_smoke():
    url = os.environ.get("AITE_DECIDER_URL", "http://127.0.0.1:8000")
    client = DeciderClient(url=url, timeout_s=30, connect_timeout_s=5)
    gate = ConfidenceGate()

    response = client.ask(
        state="Smoke test state for decider_gate's live check.",
        questions={
            "smoke_choice": {
                "type": "choice",
                "instructions": "Pick option A or option B.",
                "criteria": {"A": "The first option", "B": "The second option"},
            },
            "smoke_noul": {
                "type": "noul",
                "instructions": "Is this a smoke test?",
                "criteria": {"true": "Yes, this is a smoke test", "false": "No"},
            },
        },
    )

    choice_verdict = gate.evaluate(
        "smoke_choice",
        response["answers"]["smoke_choice"],
        response["latency_ms"],
        host_label="live",
        model=response["model"],
    )
    noul_verdict = gate.evaluate(
        "smoke_noul",
        response["answers"]["smoke_noul"],
        response["latency_ms"],
        host_label="live",
        model=response["model"],
    )

    print(f"\nmodel: {response['model']}")
    print(f"usage: {response['usage']}")
    print(f"latency_ms (self-measured round trip): {response['latency_ms']}")
    for verdict in (choice_verdict, noul_verdict):
        print(
            f"\n{verdict.question}: answer={verdict.answer!r} suggestion={verdict.suggestion!r} "
            f"confidence={verdict.confidence:.4f} route={verdict.route} "
            f"probabilities={verdict.probabilities!r} host_label={verdict.host_label} "
            f"model={verdict.model} latency_ms={verdict.latency_ms}"
        )

    assert choice_verdict.confidence is not None
    assert 0.0 <= choice_verdict.confidence <= 1.0
    assert noul_verdict.confidence is not None
    assert 0.0 <= noul_verdict.confidence <= 1.0
