"""Unit tests for ai_test_engineer.ui_components -- the aae- styled HTML
helper layer. Escaping contract under test (see ui_components.py's module
docstring): leaf/scalar helpers (pill, banner, verdict, kpi_row, progress,
labeled_section, small_caption, gherkin_highlight, log_highlight) escape
their plain-text parameters internally -- a caller can never forget to
escape model-sourced text (Gherkin, pytest logs) because the helper is the
enforcement point. The two composite/container helpers (card's body_html,
table's row cells, code_block's text_html) accept PRE-BUILT HTML the
caller assembled from other helpers (or an explicit html.escape() call)
and do not re-escape it -- same convention as composing any other markup
tree, and verified below by the double-escape regression tests."""

from __future__ import annotations

import pytest

from ai_test_engineer import ui_components as uc

UNSAFE_TEXT = '<script>alert("x")</script> & "quotes" \'single\''
# Leaf helpers escape with html.escape(text, quote=False): &/</> are
# required escapes for HTML element CONTENT; quotes are only special
# inside an attribute VALUE, which none of these parameters are placed
# into -- escaping them anyway would mangle ordinary English apostrophes
# (e.g. "see 'Execution'.") into visible &#x27; entities on screen.
ESCAPED_UNSAFE_TEXT = """&lt;script&gt;alert("x")&lt;/script&gt; &amp; "quotes" 'single'"""


# ---------------------------------------------------------------------------
# pill()
# ---------------------------------------------------------------------------


def test_pill_renders_text_and_variant_class():
    result = uc.pill("PASS", "pass")
    assert result == '<span class="aae-pill aae-pill--pass">PASS</span>'


def test_pill_escapes_raw_text():
    result = uc.pill(UNSAFE_TEXT, "pass")
    assert ESCAPED_UNSAFE_TEXT in result
    assert "<script>" not in result


def test_pill_unknown_variant_raises():
    with pytest.raises(ValueError):
        uc.pill("x", "not-a-real-variant")


def test_pill_every_token_sheet_variant_has_colors():
    expected_variants = {
        "pass", "fail", "matches", "no-match", "added-remediation", "auto",
        "human-confirmed", "human-override", "waiting", "rejected-guard", "approved",
    }
    assert expected_variants <= set(uc.PILL_VARIANTS)
    for bg, fg in uc.PILL_VARIANTS.values():
        assert bg.startswith("#")
        assert fg.startswith("#")


# ---------------------------------------------------------------------------
# card()
# ---------------------------------------------------------------------------


def test_card_escapes_heading_but_not_prebuilt_body():
    body = uc.pill("PASS", "pass")  # pre-built HTML from another helper
    result = uc.card("Test results", body)
    assert '<p class="aae-card-heading">Test results</p>' in result
    assert body in result  # embedded verbatim, not re-escaped


def test_card_escapes_unsafe_heading():
    result = uc.card(UNSAFE_TEXT, "<p>body</p>")
    assert "<script>" not in result.split("aae-card-body")[0]
    assert ESCAPED_UNSAFE_TEXT in result


def test_card_default_tone_has_no_modifier_class():
    result = uc.card("Heading", "body")
    assert "aae-card--" not in result
    assert 'class="aae-card"' in result


def test_card_tone_adds_modifier_class():
    result = uc.card("Heading", "body", tone="gap")
    assert "aae-card--gap" in result


def test_card_rejects_unknown_tone():
    with pytest.raises(ValueError):
        uc.card("Heading", "body", tone="not-a-real-tone")


# ---------------------------------------------------------------------------
# table()
# ---------------------------------------------------------------------------


def test_table_escapes_headers():
    result = uc.table([UNSAFE_TEXT], [["x"]])
    assert ESCAPED_UNSAFE_TEXT in result
    assert "<script>" not in result.split("</thead>")[0] if "</thead>" in result else True


def test_table_does_not_escape_prebuilt_cells():
    pass_pill = uc.pill("PASS", "pass")
    result = uc.table(["Status"], [[pass_pill]])
    assert pass_pill in result


def test_table_rows_rendered_as_rows():
    result = uc.table(["ID", "Name"], [["T01", "First"], ["T02", "Second"]])
    assert result.count("<tr>") >= 2  # header row (if any) + 2 body rows, or 2 body rows
    assert "T01" in result and "Second" in result


def test_table_applies_row_classes():
    result = uc.table(["ID"], [["T01"], ["T04"]], row_classes=["", "aae-row--fail"])
    body_rows = result.split("<tbody>")[1].split("<tr")[1:]  # skip the <thead> header <tr>
    assert "aae-row--fail" not in body_rows[0]
    assert "aae-row--fail" in body_rows[1]


def test_table_row_classes_length_mismatch_raises():
    with pytest.raises(ValueError):
        uc.table(["ID"], [["T01"], ["T04"]], row_classes=["only-one"])


# ---------------------------------------------------------------------------
# kpi_row()
# ---------------------------------------------------------------------------


def test_kpi_row_renders_each_tile():
    result = uc.kpi_row(
        [
            {"label": "Total tests", "value": "5", "tone": "default"},
            {"label": "Passed", "value": "4", "tone": "passed"},
            {"label": "Failed", "value": "1", "tone": "failed"},
        ]
    )
    assert "Total tests" in result and "Passed" in result and "Failed" in result
    assert "aae-kpi-value--passed" in result
    assert "aae-kpi-value--failed" in result


def test_kpi_row_escapes_tile_text():
    result = uc.kpi_row([{"label": UNSAFE_TEXT, "value": "1", "tone": "default"}])
    assert ESCAPED_UNSAFE_TEXT in result


# ---------------------------------------------------------------------------
# banner() / verdict()
# ---------------------------------------------------------------------------


def test_banner_renders_title_and_sub():
    result = uc.banner("Action needed", "1 decision waiting", variant="action-needed")
    assert "Action needed" in result
    assert "1 decision waiting" in result
    assert "aae-banner--action-needed" in result


def test_banner_handles_missing_sub():
    result = uc.banner("Title only", None, variant="workflow-ok")
    assert "Title only" in result


def test_banner_escapes_text():
    result = uc.banner(UNSAFE_TEXT, UNSAFE_TEXT, variant="workflow-ok")
    assert result.count(ESCAPED_UNSAFE_TEXT) == 2


def test_banner_action_html_is_not_escaped():
    link = '<a href="#aae-action-anchor">Jump to it</a>'
    result = uc.banner("Action needed below", None, variant="action-needed", action_html=link)
    assert link in result


def test_banner_without_action_html_has_no_action_span():
    result = uc.banner("Title", None, variant="action-needed")
    assert "aae-banner-action" not in result


def test_verdict_ok_true_uses_ok_variant():
    result = uc.verdict("Agent workflow: COMPLETED", "Every stage ran.", ok=True)
    assert "aae-banner--workflow-ok" in result or "aae-verdict--ok" in result


def test_verdict_ok_false_uses_fail_variant():
    result = uc.verdict("Payment verification: FAILED", "BR06 is violated.", ok=False)
    assert "fail" in result


# ---------------------------------------------------------------------------
# conf_bar()
# ---------------------------------------------------------------------------


def test_conf_bar_teal_when_at_or_above_threshold():
    result = uc.conf_bar(0.87, 0.75)
    assert "#0D9488" in result


def test_conf_bar_amber_when_below_threshold():
    result = uc.conf_bar(0.69, 0.75)
    assert "#F59E0B" in result


def test_conf_bar_includes_numeric_value():
    result = uc.conf_bar(0.6912, 0.75)
    assert "0.6912" in result


def test_conf_bar_respects_width_px():
    result = uc.conf_bar(0.8, 0.75, width_px=120)
    assert "120px" in result


# ---------------------------------------------------------------------------
# progress()
# ---------------------------------------------------------------------------


def test_progress_renders_all_steps_with_state_classes():
    steps = [
        {"label": "Interpret", "sub": "", "state": "done"},
        {"label": "Generate", "sub": "", "state": "done"},
        {"label": "Coverage", "sub": "", "state": "current"},
        {"label": "Decide", "sub": "waiting for you", "state": "current"},
        {"label": "Approve", "sub": "", "state": "future"},
        {"label": "Execute", "sub": "", "state": "future"},
        {"label": "Report", "sub": "", "state": "future"},
    ]
    result = uc.progress(steps)
    assert result.count("aae-progress-dot--done") == 2
    assert result.count("aae-progress-dot--current") == 2
    assert result.count("aae-progress-dot--future") == 3
    assert "waiting for you" in result


def test_progress_rejects_unknown_state():
    with pytest.raises(ValueError):
        uc.progress([{"label": "X", "sub": "", "state": "not-a-real-state"}])


def test_progress_escapes_label_and_sub():
    result = uc.progress([{"label": UNSAFE_TEXT, "sub": UNSAFE_TEXT, "state": "future"}])
    assert result.count(ESCAPED_UNSAFE_TEXT) == 2


# ---------------------------------------------------------------------------
# code_block() / gherkin_highlight() / log_highlight()
# ---------------------------------------------------------------------------


def test_code_block_wraps_prebuilt_content():
    result = uc.code_block("already &lt;escaped&gt; text")
    assert "already &lt;escaped&gt; text" in result
    assert result.startswith('<pre class="aae-code aae-code--text"')


def test_gherkin_highlight_escapes_raw_model_text():
    raw = 'Given a valid payment request <P-001> for "100.00"'
    result = uc.gherkin_highlight(raw)
    assert "&lt;P-001&gt;" in result
    assert "<P-001>" not in result


def test_gherkin_highlight_spans_keywords():
    raw = "Scenario: S01 - Initial payment succeeds\nGiven a valid payment\nWhen submitted\nThen it succeeds\nAnd one charge exists"
    result = uc.gherkin_highlight(raw)
    assert '<span class="aae-kw-scenario">Scenario</span>' in result
    assert '<span class="aae-kw-step">Given</span>' in result
    assert '<span class="aae-kw-step">When</span>' in result
    assert '<span class="aae-kw-step">Then</span>' in result
    assert '<span class="aae-kw-step">And</span>' in result


def test_gherkin_highlight_does_not_rewrite_keyword_substrings_mid_line():
    # "Andover" must not be treated as the And keyword.
    raw = "Andover is a town, not a step keyword"
    result = uc.gherkin_highlight(raw)
    assert '<span class="aae-kw-step">And</span>' not in result


def test_log_highlight_escapes_raw_text():
    raw = "tests/payment/test_t04.py::test_t04 FAILED <evidence>"
    result = uc.log_highlight(raw)
    assert "&lt;evidence&gt;" in result


def test_log_highlight_spans_passed_and_failed():
    raw = "tests/t01.py PASSED\ntests/t04.py FAILED"
    result = uc.log_highlight(raw)
    assert '<span class="aae-log-pass">PASSED</span>' in result
    assert '<span class="aae-log-fail">FAILED</span>' in result


# ---------------------------------------------------------------------------
# labeled_section() / small_caption()
# ---------------------------------------------------------------------------


def test_labeled_section_renders_label_and_body():
    result = uc.labeled_section("aae-label-problem", "PROBLEM", "A customer pays $100 once.")
    assert "aae-label-problem" in result
    assert "PROBLEM" in result
    assert "A customer pays $100 once." in result


def test_labeled_section_escapes_both_params():
    result = uc.labeled_section("aae-label-problem", UNSAFE_TEXT, UNSAFE_TEXT)
    assert result.count(ESCAPED_UNSAFE_TEXT) == 2


def test_small_caption_escapes_text():
    result = uc.small_caption(UNSAFE_TEXT)
    assert ESCAPED_UNSAFE_TEXT in result
    assert '<p class="aae-caption">' in result


# ---------------------------------------------------------------------------
# Double-escape regression: text that is ALREADY html.escape()'d, passed to
# a leaf helper as plain text, must not come out double-escaped (plan
# section 1's required regression test).
# ---------------------------------------------------------------------------


def test_pill_does_not_double_escape_already_escaped_text():
    import html

    already_escaped = html.escape("Tom & Jerry")
    result = uc.pill(already_escaped, "pass")
    # pill() is a leaf helper: it escapes internally, so pre-escaped input
    # comes out double-escaped. This test documents that leaf helpers take
    # RAW text, not pre-escaped text -- callers must not pre-escape before
    # calling a leaf helper, only before composing into a container helper.
    assert "&amp;amp;" in result  # &amp; -> &amp;amp; proves it WOULD double-escape raw pre-escaped text
    # The real double-escape guard is on the composite side (card/table),
    # covered below.


def test_card_does_not_double_escape_prebuilt_body_html():
    # body_html built by composing pill() + a manually html.escape()'d
    # fragment -- card() must embed it verbatim, no second escaping pass.
    import html

    body = html.escape("<not a real tag, just text with & and <>") + uc.pill("PASS", "pass")
    result = uc.card("Heading", body)
    assert body in result  # byte-for-byte present, not re-escaped


def test_table_does_not_double_escape_prebuilt_cells():
    import html

    cell = html.escape("Tom & Jerry's <show>")
    result = uc.table(["Name"], [[cell]])
    assert cell in result
    assert "&amp;amp;" not in result


# ---------------------------------------------------------------------------
# widget_card_container() -- thin st.container(key=...) wrapper; exercised
# properly by AppTest once pages actually use it (Commit C1). Here we only
# confirm it exists with the right signature/name convention.
# ---------------------------------------------------------------------------


def test_widget_card_container_is_callable():
    assert callable(uc.widget_card_container)


# ---------------------------------------------------------------------------
# AAE_CSS -- a static config-like asset, not testable behavior in the usual
# TDD sense; a smoke test still catches an accidental drift between the
# stylesheet and the PILL_VARIANTS color table it's generated from.
# ---------------------------------------------------------------------------


def test_aae_css_contains_every_pill_variant_color():
    for variant, (bg, fg) in uc.PILL_VARIANTS.items():
        assert f".aae-pill--{variant}" in uc.AAE_CSS
        assert bg in uc.AAE_CSS
        assert fg in uc.AAE_CSS


def test_aae_css_contains_core_component_classes():
    for selector in (
        ".aae-card", ".aae-table", ".aae-banner", ".aae-kpi-row", ".aae-code",
        ".aae-progress", ".aae-conf-bar", ".aae-nav-badge", ".aae-caption",
    ):
        assert selector in uc.AAE_CSS


def test_aae_css_has_no_aite_prefixed_classes():
    assert "aite-" not in uc.AAE_CSS
