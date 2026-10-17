"""Unit tests for ai_test_engineer.orchestration's static prompt builders.

Only the pure, no-agent-call prompt builders are tested here -- the rest of
orchestration.py drives a real Strands Agent/Bedrock and is exercised by the
live run (scripts/run_initial_pass.py), not unit tests.
"""

from __future__ import annotations

from ai_test_engineer.orchestration import _interpretation_prompt


def test_interpretation_prompt_scopes_critical_to_s01_s05():
    prompt = _interpretation_prompt("some requirement text")

    assert "S01" in prompt and "S05" in prompt
    assert "T01" in prompt and "T05" in prompt
    assert "critical ONLY if" in prompt
    assert "scripted" in prompt.lower() and "deterministic" in prompt.lower()


def test_interpretation_prompt_lists_out_of_scope_topics_as_non_critical_not_omitted():
    prompt = _interpretation_prompt("some requirement text")

    for topic in ("key generation ownership", "TTLs", "non-amount payload fields"):
        assert topic in prompt
    assert "non-critical" in prompt
    assert "never suppress" in prompt
    assert "omitting" in prompt
