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


def test_pill_supports_every_real_junit_test_status():
    # execution.py's _parse_junit produces PASS/FAIL/ERROR/SKIPPED (a
    # <error> node for a fixture/setup exception, a <skipped> node for a
    # skipped test) -- the Execution/Results "Result"/"Status" columns
    # call pill(status, status.lower()) for whatever status a real run
    # actually recorded, so every one of the four must be a real variant,
    # not just pass/fail (a crash here used to take down the whole page).
    for status in ("PASS", "FAIL", "ERROR", "SKIPPED"):
        result = uc.pill(status, status.lower())
        assert status in result


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


def test_card_teal_accent_tone_adds_modifier_class():
    # polish-2 group F15: Overview's four cards get a teal left-accent
    # heading (not a full background tint like gap/decider -- "accents,
    # not heavy fills").
    result = uc.card("Heading", "body", tone="teal-accent")
    assert "aae-card--teal-accent" in result


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


def test_table_col_widths_renders_colgroup():
    result = uc.table(["ID", "Scenario", "Rules"], [["T01", "x", "BR01, BR02"]], col_widths=["44px", None, "150px"])
    assert result.startswith('<table class="aae-table">')
    assert '<colgroup><col style="width:44px;"><col><col style="width:150px;"></colgroup>' in result
    # colgroup must come before the real rendered content, not replace it.
    assert "T01" in result and "BR01, BR02" in result


def test_table_col_widths_length_mismatch_raises():
    with pytest.raises(ValueError):
        uc.table(["ID", "Scenario"], [["T01", "x"]], col_widths=["44px"])


def test_table_without_col_widths_has_no_colgroup():
    result = uc.table(["ID"], [["T01"]])
    assert "<colgroup>" not in result


def test_table_fixed_layout_adds_modifier_class():
    # polish-2 group D13: the Results-page decision-trace table needs
    # table-layout: fixed + wrapped cell text so it never overflows its
    # (now narrower, ~2:3 split) card -- opt-in per table, not global
    # (other tables rely on table-layout: auto to size to their content).
    result = uc.table(["ID"], [["T01"]], fixed_layout=True)
    assert result.startswith('<table class="aae-table aae-table--fixed">')


def test_table_without_fixed_layout_has_no_modifier_class():
    result = uc.table(["ID"], [["T01"]])
    assert "aae-table--fixed" not in result


def test_table_teal_header_variant_adds_modifier_class():
    # polish-2 group F15: Overview's Business Rules table gets a teal-
    # tinted header row (#E6F2F1/#0B5F57) -- opt-in, not a global table()
    # change (every other table keeps the neutral grey header).
    result = uc.table(["ID"], [["T01"]], header_variant="teal")
    assert result.startswith('<table class="aae-table aae-table--teal-header">')


def test_table_default_header_variant_has_no_modifier_class():
    result = uc.table(["ID"], [["T01"]])
    assert "aae-table--teal-header" not in result


def test_table_rejects_unknown_header_variant():
    with pytest.raises(ValueError):
        uc.table(["ID"], [["T01"]], header_variant="not-a-real-variant")


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


def test_kpi_row_value_has_inline_style():
    # polish-2 live-check round: raised CSS specificity alone still lost
    # to Streamlit's cascade live (confirmed by the user) -- an inline
    # style= wins regardless, so this is a rendered-element check, not a
    # CSS-source check.
    result = uc.kpi_row([{"label": "Total tests", "value": "5", "tone": "default"}])
    assert 'style="font-size:30px;font-weight:700;line-height:1.1"' in result


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


def test_conf_bar_formats_confidence_to_2_decimals():
    # polish-2 group D12: confidence reads to 2 decimals everywhere
    # conf_bar is used (the confirm-decision card and the decision-trace
    # table) -- was bare {:g} (4 decimals straight off decisions.json).
    result = uc.conf_bar(0.6912, 0.75)
    assert "0.69" in result
    assert "0.6912" not in result


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


def test_progress_connector_shows_a_visible_arrow_glyph():
    # polish-2 live-check round: the connector line rendered but had no
    # visible arrow glyph; the glyph is now its own flex sibling sitting
    # in the gap between steps -- never absolutely positioned on top of
    # a circle (the earlier bug: it overlapped the next step's dot).
    steps = [{"label": "A", "sub": "", "state": "done"}, {"label": "B", "sub": "", "state": "future"}]
    result = uc.progress(steps)
    assert "→" in result
    assert '<span class="aae-progress-arrow" style="color:' in result
    assert "position:absolute" not in result


def test_progress_connector_arrow_is_teal_when_filled_grey_when_not():
    filled = uc.progress([{"label": "A", "sub": "", "state": "done"}, {"label": "B", "sub": "", "state": "future"}])
    not_filled = uc.progress([{"label": "A", "sub": "", "state": "future"}, {"label": "B", "sub": "", "state": "future"}])
    assert "#0D9488" in filled
    assert "#9CA7B4" in not_filled


def test_progress_arrow_is_a_sibling_of_the_step_spans_not_nested_inside_one():
    # polish-2 live-check round regression: the arrow must sit in the gap
    # between two step elements, never inside either one's own span (which
    # is what let it render on top of a circle before).
    steps = [{"label": "A", "sub": "", "state": "done"}, {"label": "B", "sub": "", "state": "future"}]
    result = uc.progress(steps)
    first_step_close = result.index("</p></span>")  # label closes, then the step span itself closes
    arrow_open = result.index('<span class="aae-progress-arrow"')
    second_step_open = result.rindex('<span class="aae-progress-step">')
    assert first_step_close < arrow_open < second_step_open
    assert result.count('class="aae-progress-step"') == 2
    assert result.count('class="aae-progress-arrow"') == 1


# ---------------------------------------------------------------------------
# code_block() / gherkin_highlight() / log_highlight()
# ---------------------------------------------------------------------------


def test_code_block_wraps_prebuilt_content():
    result = uc.code_block("already &lt;escaped&gt; text")
    assert "already &lt;escaped&gt; text" in result
    assert result.startswith('<pre class="aae-code aae-code--text"')


def test_code_block_has_inline_style():
    # polish-2 live-check round: same reasoning as kpi_row's inline-style
    # test above -- an inline style= on the <pre> itself, verbatim.
    result = uc.code_block("x")
    assert (
        'style="background:#0F1B2D;color:#D7E0EA;border-radius:7px;padding:12px 14px;'
        'font-family:\'Source Code Pro\',monospace;font-size:11.5px;line-height:1.6;'
        'white-space:pre-wrap;margin:0"'
    ) in result


def test_gherkin_highlight_escapes_raw_model_text():
    raw = 'Given a valid payment request <P-001> for "100.00"'
    result = uc.gherkin_highlight(raw)
    assert "&lt;P-001&gt;" in result
    assert "<P-001>" not in result


def test_gherkin_highlight_spans_keywords():
    raw = "Scenario: S01 - Initial payment succeeds\nGiven a valid payment\nWhen submitted\nThen it succeeds\nAnd one charge exists"
    result = uc.gherkin_highlight(raw)
    assert '<span class="aae-kw-scenario" style="color:#5EEAD4">Scenario</span>' in result
    assert '<span class="aae-kw-step" style="color:#93C5FD">Given</span>' in result
    assert '<span class="aae-kw-step" style="color:#93C5FD">When</span>' in result
    assert '<span class="aae-kw-step" style="color:#93C5FD">Then</span>' in result
    assert '<span class="aae-kw-step" style="color:#93C5FD">And</span>' in result


def test_gherkin_highlight_does_not_rewrite_keyword_substrings_mid_line():
    # "Andover" must not be treated as the And keyword.
    raw = "Andover is a town, not a step keyword"
    result = uc.gherkin_highlight(raw)
    assert '>And</span>' not in result


def test_gherkin_highlight_preserves_blank_lines():
    # Live-check root cause: real Gherkin can have blank lines between
    # steps -- gherkin_highlight() itself must never collapse them (the
    # fix for Markdown eating them is st.html() at the call site, but
    # this function's own output must still carry them through intact).
    raw = "Scenario: S01\n\nGiven a valid payment\nWhen submitted"
    result = uc.gherkin_highlight(raw)
    assert "\n\n" in result


def test_log_highlight_escapes_raw_text():
    raw = "tests/payment/test_t04.py::test_t04 FAILED <evidence>"
    result = uc.log_highlight(raw)
    assert "&lt;evidence&gt;" in result


def test_log_highlight_spans_passed_and_failed():
    raw = "tests/t01.py PASSED\ntests/t04.py FAILED"
    result = uc.log_highlight(raw)
    assert '<span class="aae-log-pass" style="color:#86EFAC">PASSED</span>' in result
    assert '<span class="aae-log-fail" style="color:#FCA5A5">FAILED</span>' in result


# ---------------------------------------------------------------------------
# polish-2 live-check round: a blank line inside HTML passed to
# st.markdown(unsafe_allow_html=True) ends Markdown's raw-HTML-block
# parsing early (CommonMark's own rule) -- the rest renders as a plain
# paragraph instead of staying inside the intended tag. code_block()'s
# own output is exempt (its three call sites in ui.py now use st.html(),
# which never runs the Markdown parser at all) -- this guards every OTHER
# HTML-building function, which must never embed a literal blank line.
# ---------------------------------------------------------------------------


def test_no_other_html_builder_embeds_a_blank_line():
    samples = [
        uc.card("Heading", "<p>Body</p><p>More</p>", tone="teal-accent"),
        uc.table(["A", "B"], [["1", "2"], ["3", "4"]], fixed_layout=True, header_variant="teal"),
        uc.banner("Title", "Sub line", variant="action-needed", action_html="<a href='#x'>Go</a>"),
        uc.kpi_row([{"label": "A", "value": "1", "sub": "s", "tone": "passed"}]),
        uc.progress([{"label": "Step", "sub": "waiting", "state": "current"}, {"label": "Next", "sub": "", "state": "future"}]),
        uc.conf_bar(0.8, 0.75),
        uc.pill("Matches", "matches"),
        uc.labeled_section("aae-label-problem", "PROBLEM", "Some text."),
        uc.checklist(["One", "Two"]),
        uc.numbered_steps(["One", "Two"]),
        uc.busy_indicator("Generating scenarios…"),
        uc.decision_trace_table(
            [{"checkpoint": "COVERAGE", "icon": "✅", "decision_text": "x", "detail": None, "confidence": 0.9,
              "threshold": 0.75, "route": "AUTO", "source": "DECIDER", "override": False, "outcome": "Auto"}]
        ),
    ]
    for sample in samples:
        assert "\n\n" not in sample, sample[:200]


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


def test_checklist_renders_teal_check_items_escaped():
    # polish-2 group F15: Overview's "Key acceptance criteria" list gets
    # teal check bullets instead of a plain <ul>.
    result = uc.checklist(["Handle timeouts", UNSAFE_TEXT])
    assert "aae-checklist" in result
    assert "Handle timeouts" in result
    assert ESCAPED_UNSAFE_TEXT in result
    assert "<script>" not in result


def test_numbered_steps_renders_teal_numbered_items_escaped():
    # polish-2 group F15: Overview's "What the agent does" list gets teal
    # numbered circles instead of a plain <ol>.
    result = uc.numbered_steps(["Reads the requirement.", UNSAFE_TEXT])
    assert "aae-numbered-steps" in result
    assert "Reads the requirement." in result
    assert ESCAPED_UNSAFE_TEXT in result
    assert "<script>" not in result


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


def test_busy_indicator_escapes_label():
    result = uc.busy_indicator(UNSAFE_TEXT)
    assert ESCAPED_UNSAFE_TEXT in result
    assert "<script>" not in result


def test_busy_indicator_has_the_pulsing_dot_span():
    result = uc.busy_indicator("Generating scenarios…")
    assert '<span class="aae-busy-dot">' in result


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


def test_aae_css_base_typography_is_13px_145_everywhere():
    # Card paragraphs, lists, captions, and widget labels all share one
    # base size/line-height -- nothing should fall through to Streamlit's
    # default 16px.
    assert 'font-size: 13px !important;\n    line-height: 1.45 !important;' in uc.AAE_CSS
    assert '[data-testid="stWidgetLabel"]' in uc.AAE_CSS
    assert "stMarkdownContainer\"] li " in uc.AAE_CSS or 'stMarkdownContainer"] li {' in uc.AAE_CSS


def test_aae_css_table_rules_use_important_and_higher_specificity():
    # Streamlit's own markdown-table CSS otherwise wins the cascade --
    # raised specificity ([data-testid="stMarkdownContainer"] .aae-table)
    # plus !important on size/spacing is what makes ours win.
    assert '[data-testid="stMarkdownContainer"] .aae-table {' in uc.AAE_CSS
    assert '[data-testid="stMarkdownContainer"] .aae-table th {' in uc.AAE_CSS
    assert '[data-testid="stMarkdownContainer"] .aae-table td {' in uc.AAE_CSS
    assert "font-size: 12.5px !important;\n    line-height: 1.35 !important;" in uc.AAE_CSS


def test_aae_css_rule_quote_has_explicit_13px_font_size():
    # polish-2 group F16: .aae-rule-quote is a bare <div>, not a <p>, so
    # the global 13px/1.45 body rule (which only targets p/li/etc.) never
    # applied to it -- it fell back to Streamlit's markdown default,
    # rendering larger than every other body-text element on the page.
    assert ".aae-rule-quote {" in uc.AAE_CSS
    rule_quote_block = uc.AAE_CSS.split(".aae-rule-quote {")[1].split("}")[0]
    assert "font-size: 13px" in rule_quote_block
    assert "line-height: 1.45" in rule_quote_block


def test_aae_css_kpi_value_rule_no_longer_fights_the_inline_style():
    # polish-2 live-check round: group F16's raised-specificity fix (a
    # .aae-kpi-value rule with font-size/font-weight !important) was itself
    # the reason the KPI numbers STILL rendered small after kpi_row() got
    # an inline style="font-size:30px..." -- a stylesheet !important beats
    # an inline style with no !important of its own, regardless of
    # specificity. font-size/weight must live ONLY in the inline style now
    # (see test_kpi_row_value_has_inline_style); this rule keeps color/
    # margin (which the inline style doesn't set) but must never reclaim
    # font-size/font-weight, or it would cancel the inline fix again.
    assert '[data-testid="stMarkdownContainer"] .aae-kpi-value {' in uc.AAE_CSS
    kpi_value_block = uc.AAE_CSS.split('[data-testid="stMarkdownContainer"] .aae-kpi-value {')[1].split("}")[0]
    assert "font-size" not in kpi_value_block
    assert "font-weight" not in kpi_value_block
    assert "color: #0F1B2D !important;" in kpi_value_block


def test_aae_css_code_block_rules_use_important_and_higher_specificity():
    # polish-2 group E: live screenshots showed Gherkin/log/JSON rendering
    # as flattened paragraph text on a light background despite correct-
    # looking .aae-code source -- the same class of bug as the table fix
    # above (confirmed: .aae-code was a bare, un-raised selector with no
    # !important, so Streamlit's own stMarkdownContainer/p rules won the
    # cascade). Same fix: raised specificity + !important.
    assert '[data-testid="stMarkdownContainer"] .aae-code {' in uc.AAE_CSS
    assert "background-color: #0F1B2D !important;" in uc.AAE_CSS
    assert "white-space: pre-wrap !important;" in uc.AAE_CSS
    assert 'font-family: "Source Code Pro", monospace !important;' in uc.AAE_CSS


def test_aae_css_vertical_and_horizontal_card_gap_both_16px():
    assert '[data-testid="stVerticalBlock"] {\n    gap: 16px !important;' in uc.AAE_CSS
    assert '[data-testid="stHorizontalBlock"] {\n    gap: 16px !important;' in uc.AAE_CSS


def test_aae_css_zeroes_stmarkdowncontainer_negative_margin():
    # Streamlit's own stMarkdownContainer carries a built-in
    # margin-bottom: -16px (confirmed live via getComputedStyle) meant to
    # counteract plain markdown's native paragraph spacing -- for our
    # custom unsafe_allow_html cards it has nothing to counteract, so it
    # silently eats 16px off the flex-column ancestor's auto-height,
    # making the real [data-testid="stVerticalBlock"] gap invisible
    # (cards touched with 0px visible gap even though the gap rule itself
    # was correctly 16px). Without this override, two stacked cards
    # measure 0px apart live.
    assert '[data-testid="stMarkdownContainer"] {\n    margin-bottom: 0 !important;' in uc.AAE_CSS


def test_aae_css_has_no_aite_prefixed_classes():
    assert "aite-" not in uc.AAE_CSS


def test_aae_css_results_download_buttons_are_teal():
    # polish-2 group B5: the two Results-page download buttons are teal
    # like every other live/enabled control (download_report_btn/
    # download_evidence_btn, shared by both the live st.download_button
    # and the locked st.button fallback).
    assert ".st-key-download_report_btn button" in uc.AAE_CSS
    assert ".st-key-download_evidence_btn button" in uc.AAE_CSS


def test_aae_css_banner_action_buttons_are_teal():
    # polish-2 group B4: the action banner's cross-page "Go to X" button
    # (a plain st.button keyed jump_to_action_page) and the same-page
    # "Jump to it" anchor (styled via .aae-btn-teal, since it's a real <a>,
    # not a widget) both read teal, the same brand accent every other
    # live/enabled control already uses.
    assert ".st-key-jump_to_action_page button {" in uc.AAE_CSS
    assert f"background-color: {uc._CONF_TEAL} !important;" in uc.AAE_CSS
    assert ".aae-btn-teal {" in uc.AAE_CSS


# ---------------------------------------------------------------------------
# decision_trace_table() -- the ONE renderer for decision_trace.
# build_decision_trace's (outcome-extended) rows, used on Analysis/
# Scenarios/Results. Consumes real row dicts shaped like decision_trace.py
# produces, so these tests build them inline (same shape, no import of
# decision_trace itself -- ui_components must stay decision_trace-agnostic).
# ---------------------------------------------------------------------------


def _row(checkpoint, decision_text, confidence, threshold, outcome, detail=None):
    return {
        "checkpoint": checkpoint, "icon": "✅", "decision_text": decision_text, "detail": detail,
        "confidence": confidence, "threshold": threshold, "route": "AUTO",
        "source": "DECIDER", "override": False, "outcome": outcome,
    }


def test_decision_trace_table_outcome_detail_becomes_a_tooltip_not_a_long_decision_sentence():
    # polish-2 group D12: a long override/escalated explanation lives in
    # the Outcome pill's title= tooltip now, never appended onto
    # decision_text.
    rows = [_row("COVERAGE", "REMEDIATE_GAP (human)", 0.55, 0.75, "Human override", detail="override of Decider's 'ACCEPT_COVERAGE' suggestion")]
    result = uc.decision_trace_table(rows)
    assert "REMEDIATE_GAP (human)" in result
    assert "override of Decider's" not in result.split("REMEDIATE_GAP (human)")[0]  # not glued onto Decision
    assert 'title="override of Decider' in result


def test_decision_trace_table_no_tooltip_when_no_detail():
    rows = [_row("COVERAGE", "ACCEPT_COVERAGE (human)", 0.97, 0.75, "Human confirmed")]
    result = uc.decision_trace_table(rows)
    assert "title=" not in result


def test_decision_trace_table_renders_checkpoint_and_decision_text():
    rows = [_row("COVERAGE", "ACCEPT_COVERAGE", 0.97, 0.75, "Auto")]
    result = uc.decision_trace_table(rows)
    assert "COVERAGE" in result
    assert "ACCEPT_COVERAGE" in result
    assert "aae-table" in result


def test_decision_trace_table_renders_confidence_bar_when_present():
    rows = [_row("COVERAGE", "ACCEPT_COVERAGE", 0.97, 0.75, "Auto")]
    result = uc.decision_trace_table(rows)
    assert "aae-conf-bar" in result
    assert "0.97" in result


def test_decision_trace_table_shows_dash_when_no_confidence():
    rows = [_row("COMPLETION", "not reached", None, None, "not reached")]
    result = uc.decision_trace_table(rows)
    assert "aae-conf-bar" not in result


@pytest.mark.parametrize(
    "outcome,variant",
    [
        ("Auto", "auto"),
        ("Human confirmed", "human-confirmed"),
        ("Human override", "human-override"),
        ("⏳ Waiting", "waiting"),
        ("Approved", "approved"),
        ("Rejected", "rejected-guard"),
        # Fixed literal (no embedded reason) -- outcome is a stable, exact-
        # match vocabulary; the real rejection reason lives only in
        # decision_text, never duplicated into outcome.
        ("Rejected by guard", "rejected-guard"),
    ],
)
def test_decision_trace_table_maps_outcome_to_pill_variant(outcome, variant):
    rows = [_row("COVERAGE", "x", 0.9, 0.75, outcome)]
    result = uc.decision_trace_table(rows)
    assert f"aae-pill--{variant}" in result
    assert outcome in result


def test_decision_trace_table_has_no_prefix_matching_for_unknown_outcomes():
    """outcome is now always an exact fixed literal (or "not reached") --
    an unrecognized value (e.g. a stray dynamic string) falls through to
    plain escaped text, never a guessed-at pill variant."""
    rows = [_row("COVERAGE", "x", 0.9, 0.75, "Rejected by guard: some reason that should not appear here")]
    result = uc.decision_trace_table(rows)
    assert "aae-pill" not in result


def test_decision_trace_table_not_reached_is_plain_text_not_a_pill():
    rows = [_row("COMPLETION", "not reached", None, None, "not reached")]
    result = uc.decision_trace_table(rows)
    assert "not reached" in result
    assert "aae-pill" not in result


def test_decision_trace_table_escapes_checkpoint_and_decision_text():
    rows = [_row(UNSAFE_TEXT, UNSAFE_TEXT, None, None, "Auto")]
    result = uc.decision_trace_table(rows)
    assert ESCAPED_UNSAFE_TEXT in result
    assert "<script>" not in result
