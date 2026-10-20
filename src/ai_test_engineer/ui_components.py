"""HTML-string rendering helpers for the aae- styled UI layer (ui.py).

Escaping contract (two kinds of parameter):

- Leaf/scalar parameters (plain strings: headings, pill text, banner
  titles/subs, kpi tile label/value, progress step label/sub, labeled_section's
  label/body, small_caption's text, and the raw text gherkin_highlight/
  log_highlight receive) are html.escape()'d INSIDE the helper. A caller
  can never forget to escape model-sourced text (Gherkin scenarios, pytest
  log output) just because a call site several layers up didn't think to
  -- the helper is the enforcement point.
- Composite/container parameters (card()'s body_html, table()'s row cells,
  code_block()'s text_html) are PRE-BUILT HTML the caller assembled by
  calling other helpers in this module (and/or an explicit html.escape()
  for a plain-text fragment) -- these are NOT re-escaped, exactly as a
  container never re-escapes markup its own children already rendered.
  table()'s HEADERS are the one exception inside table(): headers are
  always short literal column titles, so they're treated as leaf/plain
  text and escaped internally, same as every other leaf parameter.

Every helper here is a pure function returning a complete HTML string (or,
for widget_card_container, an st.container context manager) -- no
Streamlit rendering side effects of its own. Callers assemble a full
card's HTML and pass the single result to ONE st.markdown(...,
unsafe_allow_html=True) call; splitting a card's open/body/close across
two st.markdown calls makes Streamlit drop the unclosed tags.
"""

from __future__ import annotations

import html
import re

import streamlit as st

# ---------------------------------------------------------------------------
# Design tokens (see the polish-1 plan's section B for the source values).
# ---------------------------------------------------------------------------

PILL_VARIANTS: dict[str, tuple[str, str]] = {
    "pass": ("#DCF3E4", "#11602E"),
    "fail": ("#B42318", "#FFFFFF"),
    "matches": ("#E3F4EF", "#0B5F57"),
    "no-match": ("#FDECEA", "#B42318"),
    "added-remediation": ("#0D9488", "#FFFFFF"),
    "auto": ("#E6F2F1", "#0B5F57"),
    "human-confirmed": ("#E3F4EF", "#0B5F57"),
    "human-override": ("#EDE7FB", "#4B2A9B"),
    "waiting": ("#FFF1D6", "#7A4D00"),
    "rejected-guard": ("#FDECEA", "#B42318"),
    "approved": ("#DCF3E4", "#11602E"),
}

_CARD_TONES = {"default", "gap", "decider", "failure"}
_PROGRESS_STATES = {"done", "current", "future"}
_KPI_TONES = {"default", "passed", "failed"}
_BANNER_VARIANTS = {"workflow-ok", "verification-fail", "action-needed"}

_CONF_TEAL = "#0D9488"
_CONF_AMBER = "#F59E0B"


def escape(text: str) -> str:
    """The one escaping function every leaf helper uses for element
    CONTENT (text between tags, never an attribute value): html.escape(...,
    quote=False). Only &/</> are structurally required escapes inside HTML
    text content -- quote=True (the stdlib default) additionally rewrites
    " and ' to &quot;/&#x27;, which is only needed inside a quoted
    attribute value. None of this module's leaf parameters are placed into
    an attribute, so the default would visibly mangle ordinary English
    text like "see 'Execution'." into "see &#x27;Execution&#x27;." on
    screen for no safety benefit. The few genuine attribute-value cases
    (table()'s row_class, labeled_section()'s label_class -- both always
    internal CSS class literals, never user/model text) call html.escape()
    directly with its quote=True default instead of this function."""
    return html.escape(text, quote=False)


def pill(text: str, variant: str) -> str:
    """Leaf helper: text is raw/plain and escaped internally."""
    if variant not in PILL_VARIANTS:
        raise ValueError(f"unknown pill variant {variant!r}")
    return f'<span class="aae-pill aae-pill--{variant}">{escape(text)}</span>'


def card(heading: str, body_html: str, *, tone: str = "default") -> str:
    """heading is leaf/plain text (escaped internally); body_html is
    pre-built HTML the caller assembled (NOT escaped again here)."""
    if tone not in _CARD_TONES:
        raise ValueError(f"unknown card tone {tone!r}")
    tone_class = f" aae-card--{tone}" if tone != "default" else ""
    return (
        f'<div class="aae-card{tone_class}">'
        f'<p class="aae-card-heading">{escape(heading)}</p>'
        f'<div class="aae-card-body">{body_html}</div>'
        f"</div>"
    )


def table(headers: list[str], rows: list[list[str]], *, row_classes: list[str] | None = None) -> str:
    """headers are leaf/plain text (escaped internally, since they're
    always short literal column titles). Each row cell is PRE-BUILT HTML
    the caller assembled (e.g. html.escape(plain_value) or pill(...)) --
    NOT escaped again here, so a cell can embed a pill badge."""
    if row_classes is not None and len(row_classes) != len(rows):
        raise ValueError(f"row_classes length {len(row_classes)} != rows length {len(rows)}")
    classes = row_classes if row_classes is not None else [""] * len(rows)

    thead = "".join(f"<th>{escape(h)}</th>" for h in headers)
    body_rows = []
    for row, row_class in zip(rows, classes):
        class_attr = f' class="{html.escape(row_class)}"' if row_class else ""
        tds = "".join(f"<td>{cell}</td>" for cell in row)
        body_rows.append(f"<tr{class_attr}>{tds}</tr>")
    return (
        '<table class="aae-table">'
        f"<thead><tr>{thead}</tr></thead>"
        f"<tbody>{''.join(body_rows)}</tbody>"
        "</table>"
    )


def kpi_row(tiles: list[dict]) -> str:
    """Each tile's label/value are leaf/plain text, escaped internally."""
    cells = []
    for tile in tiles:
        tone = tile.get("tone", "default")
        if tone not in _KPI_TONES:
            raise ValueError(f"unknown kpi tone {tone!r}")
        value_class = f" aae-kpi-value--{tone}" if tone != "default" else ""
        cells.append(
            '<div class="aae-kpi-tile">'
            f'<p class="aae-kpi-label">{escape(tile["label"])}</p>'
            f'<p class="aae-kpi-value{value_class}">{escape(tile["value"])}</p>'
            f'<p class="aae-kpi-sub">{escape(tile.get("sub", ""))}</p>'
            "</div>"
        )
    return f'<div class="aae-kpi-row">{"".join(cells)}</div>'


def banner(title: str, sub: str | None, *, variant: str, action_html: str | None = None) -> str:
    """title/sub are leaf/plain text, escaped internally. action_html is an
    OPTIONAL pre-built HTML fragment (e.g. an <a href="#..."> anchor link)
    rendered right-aligned -- NOT escaped, since it's markup the caller
    assembled (a plain link, never a Streamlit widget: a widget needing a
    rerun, like a page-switching button, stays a real st.button laid out
    beside the banner via st.columns instead)."""
    if variant not in _BANNER_VARIANTS:
        raise ValueError(f"unknown banner variant {variant!r}")
    sub_html = f'<p class="aae-banner-sub">{escape(sub)}</p>' if sub else ""
    action = f'<span class="aae-banner-action">{action_html}</span>' if action_html else ""
    return (
        f'<div class="aae-banner aae-banner--{variant}">'
        f'<div class="aae-banner-row"><div>'
        f'<p class="aae-banner-title">{escape(title)}</p>'
        f"{sub_html}"
        f"</div>{action}</div>"
        "</div>"
    )


def verdict(title: str, sub: str | None, *, ok: bool) -> str:
    """Thin wrapper over banner() for the two Results-page verdict banners."""
    return banner(title, sub, variant="workflow-ok" if ok else "verification-fail")


def conf_bar(confidence: float, threshold: float, *, width_px: int = 90) -> str:
    """Numeric-only; no text escaping needed."""
    fill_color = _CONF_TEAL if confidence >= threshold else _CONF_AMBER
    fill_pct = max(0.0, min(1.0, confidence)) * 100
    tau_pct = max(0.0, min(1.0, threshold)) * 100
    return (
        f'<span class="aae-conf-bar">'
        f'<span class="aae-conf-track" style="width:{width_px}px;">'
        f'<span class="aae-conf-fill" style="width:{fill_pct:g}%;background:{fill_color};"></span>'
        f'<span class="aae-conf-tau" style="left:{tau_pct:g}%;"></span>'
        f"</span>"
        f'<span class="aae-conf-value">{confidence:g}</span>'
        f"</span>"
    )


def progress(steps: list[dict]) -> str:
    """Each step's label/sub are leaf/plain text, escaped internally."""
    dots = []
    total = len(steps)
    for index, step in enumerate(steps):
        state = step["state"]
        if state not in _PROGRESS_STATES:
            raise ValueError(f"unknown progress state {state!r}")
        connector = ""
        if index < total - 1:
            filled = " aae-progress-connector--filled" if state in ("done", "current") else ""
            connector = f'<span class="aae-progress-connector{filled}"></span>'
        sub_html = f'<p class="aae-progress-sub">{escape(step.get("sub", ""))}</p>' if step.get("sub") else ""
        dots.append(
            '<span class="aae-progress-step">'
            f'<span class="aae-progress-dot aae-progress-dot--{state}">{index + 1}</span>'
            f'<p class="aae-progress-label">{escape(step["label"])}</p>'
            f"{sub_html}"
            f"{connector}"
            "</span>"
        )
    return f'<div class="aae-progress">{"".join(dots)}</div>'


def code_block(text_html: str, *, language: str = "text") -> str:
    """text_html is pre-built HTML (already escaped, and already
    keyword/status-highlighted by gherkin_highlight()/log_highlight() if
    applicable) -- NOT escaped again here."""
    return f'<pre class="aae-code aae-code--{language}">{text_html}</pre>'


_GHERKIN_KEYWORD_RE = re.compile(r"^(\s*)(Scenario|Given|When|Then|And)\b")


def gherkin_highlight(raw_text: str) -> str:
    """raw_text is RAW model-generated Gherkin -- escaped internally first,
    then line-leading keyword tokens are wrapped in highlight spans. The
    keyword match runs on the ALREADY-escaped text and only at line start
    (after optional whitespace), so an escaped entity like &amp; can never
    be misread as a keyword, and a word merely containing a keyword as a
    substring (e.g. "Andover") is never rewritten."""
    escaped = escape(raw_text)
    lines = []
    for line in escaped.split("\n"):
        match = _GHERKIN_KEYWORD_RE.match(line)
        if match:
            indent, keyword = match.group(1), match.group(2)
            css_class = "aae-kw-scenario" if keyword == "Scenario" else "aae-kw-step"
            line = f'{indent}<span class="{css_class}">{keyword}</span>' + line[match.end() :]
        lines.append(line)
    return "\n".join(lines)


_LOG_STATUS_RE = re.compile(r"\b(PASSED|FAILED)\b")


def log_highlight(raw_text: str) -> str:
    """raw_text is RAW pytest log output -- escaped internally first, then
    PASSED/FAILED tokens are wrapped in status spans."""
    escaped = escape(raw_text)

    def _span(match: re.Match) -> str:
        word = match.group(1)
        css_class = "aae-log-pass" if word == "PASSED" else "aae-log-fail"
        return f'<span class="{css_class}">{word}</span>'

    return _LOG_STATUS_RE.sub(_span, escaped)


def labeled_section(label_class: str, label_text: str, body_text: str) -> str:
    """label_text/body_text are leaf/plain text, escaped internally --
    Overview's PROBLEM/IMPACT/MITIGATION sections. label_class is a CSS
    class literal going into an attribute value, so it uses html.escape()'s
    quote=True default (unlike escape()) rather than escape() -- it's
    always one of this module's own fixed class names, never dynamic text,
    but the attribute-value context is what matters here, not the source."""
    return (
        f'<p class="aae-label {html.escape(label_class)}">{escape(label_text)}</p>'
        f'<p class="aae-label-body">{escape(body_text)}</p>'
    )


def small_caption(text: str) -> str:
    """Leaf helper: text is raw/plain and escaped internally. 11px
    under-card caption (distinct from the page subtitle, which stays a
    plain st.caption call)."""
    return f'<p class="aae-caption">{escape(text)}</p>'


def _pill_css() -> str:
    """Generated from PILL_VARIANTS so the CSS and the Python color table
    can never drift apart -- one source of truth for every pill color."""
    rules = [
        f".aae-pill--{variant} {{ background:{bg}; color:{fg}; }}"
        for variant, (bg, fg) in PILL_VARIANTS.items()
    ]
    return "\n".join(rules)


# Token-driven stylesheet for the whole aae- layer (ui.py injects this once
# per run via _inject_css). The top-bar title pseudo-element is APP_TITLE-
# dependent and stays in ui.py as a small separate block, not here.
AAE_CSS = f"""
<style>
/* -- Top bar (title pseudo-element is APP_TITLE-dependent and lives in
   ui.py's own _title_css(), injected as a separate small block) -- */
[data-testid="stHeader"] {{
    background-color: #0F1B2D;
    padding: 10px 20px;
}}

/* -- Base text -- */
.block-container [data-testid="stMarkdownContainer"] p:not(.aae-card-heading) {{
    font-size: 13px !important;
    line-height: 1.5 !important;
}}

/* -- Status chips (top bar) -- */
.aae-status-chips {{
    position: fixed;
    top: 0.6rem;
    right: 1rem;
    z-index: 999999;
    display: flex;
    gap: 6px;
}}
.aae-chip {{
    font-family: "Source Code Pro", monospace;
    font-size: 11px;
    background-color: #1D3150;
    color: #D7E0EA;
    padding: 3px 9px;
    border-radius: 999px;
    white-space: nowrap;
}}

/* -- Sidebar -- */
[data-testid="stSidebar"] {{
    width: 211px !important;
    min-width: 211px !important;
    max-width: 211px !important;
}}
[data-testid="stSidebar"] [data-testid="stVerticalBlock"] {{
    gap: 6px !important;
}}
[data-testid="stSidebar"] hr {{
    border-color: #24395A !important;
}}
[class*="st-key-nav_"] button {{
    font-size: 13px !important;
    padding: 10px 12px !important;
    border-radius: 7px !important;
    color: #C9D3E0 !important;
    border: none !important;
    height: auto !important;
}}
[class*="st-key-nav_"] button > div {{
    justify-content: flex-start !important;
    gap: 10px !important;
}}
.aae-nav-badge {{
    background-color: #F59E0B;
    color: #0F1B2D;
    font-size: 11px;
    font-weight: 700;
    border-radius: 999px;
    padding: 1px 7px;
    margin-left: 6px;
}}
.st-key-refresh_status_btn button {{
    background-color: transparent !important;
    border: 1px solid #3B5578 !important;
    color: #D7E0EA !important;
    border-radius: 7px !important;
}}

/* -- Page header -- */
.block-container h1 {{
    font-size: 26px !important;
    font-weight: 700 !important;
    color: #0F1B2D !important;
}}
[data-testid="stCaptionContainer"] p {{
    font-size: 14px !important;
    color: #4A5868 !important;
}}
.aae-caption {{
    font-size: 11px;
    color: #5B6878;
    margin: 0;
}}

/* -- Card -- */
.aae-card, [class*="st-key-aae-card"] {{
    background-color: #FFFFFF;
    border: 1px solid #DDE3EA;
    border-radius: 9px;
    padding: 18px 19px;
}}
/* Transitional safety net: a handful of cards (the decision-trace rows'
   wrappers) stay native st.container(border=True) until Commit C5
   replaces their content with decision_trace_table() -- without this,
   they'd fall back to Streamlit's default spacious padding in the
   meantime. No official data-testid distinguishes a bordered
   st.container from an unbordered one in this pinned Streamlit build
   (1.66.0); re-verify this selector by inspecting a card's computed class
   if Streamlit is ever upgraded. Becomes dead CSS (matches nothing) once
   every remaining st.container(border=True) display card is migrated to
   card() -- safe to delete at that point, not required to. */
.block-container .st-emotion-cache-1te8eqs {{
    padding: 18px 19px !important;
    border-radius: 9px !important;
}}
.aae-card-heading {{
    font-size: 15px !important;
    font-weight: 700 !important;
    color: #0F1B2D !important;
    margin: 0 0 10px 0 !important;
}}
.aae-card p, [class*="st-key-aae-card"] [data-testid="stMarkdownContainer"] p {{
    margin: 0 !important;
}}
.block-container [data-testid="stHorizontalBlock"] {{
    gap: 16px !important;
}}
.aae-card--gap {{ background-color: #FFF6F5; border-color: #F1B8B2; }}
.aae-card--gap .aae-card-heading {{ color: #B42318 !important; }}
.aae-card--decider {{ background-color: #F8F5FF; border-color: #C9B8F0; }}
.aae-card--decider .aae-card-heading {{ color: #4B2A9B !important; }}
.aae-card--failure {{ border-color: #F1B8B2; }}
.aae-rule-quote {{
    background-color: #FFF7E0;
    color: #5A3E00;
    border-radius: 6px;
    padding: 7px 9px;
}}

/* -- Overview labels -- */
.aae-label {{
    font-size: 11.5px;
    font-weight: 700;
    letter-spacing: 0.06em;
    margin: 0;
}}
.aae-label-problem {{ color: #B42318; }}
.aae-label-impact {{ color: #B54708; }}
.aae-label-mitigation {{ color: #0B766C; }}
.aae-label-body {{ margin: 2px 0 10px 0 !important; }}

/* -- Table -- */
.aae-table {{
    width: 100%;
    font-size: 12.5px;
    border-collapse: collapse;
}}
.aae-table th {{
    background-color: #F0F3F7;
    color: #3A4757;
    font-weight: 600;
    text-align: left;
    padding: 8px 9px;
    border-bottom: 1px solid #DDE3EA;
}}
.aae-table td {{
    padding: 8px 9px;
    border-bottom: 1px solid #EEF1F5;
}}
.aae-row--fail {{ background-color: #FFF1F0; font-weight: 700; }}
.aae-row--added {{ background-color: #EAF6F4; }}

/* -- Pills (generated from PILL_VARIANTS) -- */
.aae-pill {{
    display: inline-block;
    font-size: 11px;
    font-weight: 700;
    border-radius: 999px;
    padding: 2px 8px;
}}
{_pill_css()}

/* -- Verdict / action banners -- */
.aae-banner {{
    border-radius: 9px;
    padding: 12px 16px;
}}
.aae-banner-row {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; }}
.aae-banner-title {{ font-size: 14px !important; font-weight: 700 !important; margin: 0 !important; }}
.aae-banner-sub {{ font-size: 11.5px !important; margin: 2px 0 0 0 !important; }}
.aae-banner-action {{ white-space: nowrap; flex-shrink: 0; }}
.aae-banner-action a {{ color: inherit; font-weight: 700; text-decoration: underline; }}
.aae-banner--workflow-ok {{ background-color: #E3F4EF; border: 1px solid #A7DCD2; color: #0B5F57; }}
.aae-banner--verification-fail {{ background-color: #FFF1F0; border: 1px solid #F1B8B2; color: #B42318; }}
.aae-banner--action-needed {{
    background-color: #FFF7E0;
    border: 1px solid #F3D48A;
    color: #5A3E00;
    border-radius: 8px;
    padding: 8px 12px;
    font-size: 12.5px;
}}
.aae-banner--action-needed .aae-banner-title {{ font-size: 12.5px !important; font-weight: 400 !important; }}

/* -- KPI tiles -- */
.aae-kpi-row {{
    display: grid;
    grid-template-columns: repeat(5, 1fr);
    gap: 12px;
}}
.aae-kpi-tile {{
    background-color: #FFFFFF;
    border: 1px solid #DDE3EA;
    border-radius: 9px;
    padding: 14px 16px;
}}
.aae-kpi-label {{ font-size: 11.5px !important; font-weight: 600 !important; color: #4A5868 !important; margin: 0 !important; }}
.aae-kpi-value {{ font-size: 26px !important; font-weight: 700 !important; color: #0F1B2D !important; margin: 2px 0 !important; }}
.aae-kpi-value--passed {{ color: #11602E !important; }}
.aae-kpi-value--failed {{ color: #B42318 !important; }}
.aae-kpi-sub {{ font-size: 11.5px !important; color: #5B6878 !important; margin: 0 !important; }}

/* -- Code / log blocks -- */
.aae-code {{
    background-color: #0F1B2D;
    color: #D7E0EA;
    border-radius: 7px;
    padding: 12px 14px;
    font-family: "Source Code Pro", monospace;
    font-size: 11.5px;
    line-height: 1.6;
    white-space: pre-wrap;
    overflow-x: auto;
}}
.aae-log-pass {{ color: #86EFAC; }}
.aae-log-fail {{ color: #FCA5A5; }}
.aae-kw-scenario {{ color: #5EEAD4; }}
.aae-kw-step {{ color: #93C5FD; }}

/* -- Tabs / segmented control -- targets Streamlit's own DOM structure
   (st.tabs/st.segmented_control emit no custom class of their own, so
   there is no aae- hook to attach to here -- re-verify these selectors
   against the actual DOM if Streamlit is ever upgraded past 1.66.0, same
   caution as ui.py's pinned-emotion-class comment elsewhere). -- */
[data-testid="stTabs"] [data-baseweb="tab"] {{
    background-color: #F0F3F7 !important;
    border: 1px solid #DDE3EA !important;
    color: #3A4757 !important;
    border-radius: 7px !important;
    padding: 7px 12px !important;
    font-size: 12.5px !important;
}}
[data-testid="stTabs"] [data-baseweb="tab"][aria-selected="true"] {{
    background-color: #0F1B2D !important;
    color: #FFFFFF !important;
}}
[data-testid="stSegmentedControl"] label {{
    background-color: #F0F3F7 !important;
    border: 1px solid #DDE3EA !important;
    color: #3A4757 !important;
    border-radius: 7px !important;
    font-size: 12.5px !important;
}}

/* -- Buttons -- */
.stButton button {{
    font-size: 13px !important;
    font-weight: 600 !important;
    border-radius: 7px !important;
    min-height: 36px !important;
}}
.st-key-approve_btn button {{
    background-color: #16803C !important;
    color: #FFFFFF !important;
    border-color: #16803C !important;
}}
.st-key-reject_btn button {{
    background-color: #FFFFFF !important;
    color: #B42318 !important;
    border: 1.5px solid #D92D20 !important;
}}
.st-key-approve_btn button:disabled,
.st-key-reject_btn button:disabled,
.st-key-execute_btn button:disabled {{
    background-color: #E6F2F1 !important;
    color: #5B7C78 !important;
    border: 1px dashed #9CC9C3 !important;
}}

/* -- Confidence bar -- */
.aae-conf-bar {{ display: inline-flex; align-items: center; gap: 8px; }}
.aae-conf-track {{
    position: relative;
    display: inline-block;
    height: 8px;
    border-radius: 999px;
    background-color: #E6EBF1;
}}
.aae-conf-fill {{
    position: absolute;
    left: 0;
    top: 0;
    height: 8px;
    border-radius: 999px;
}}
.aae-conf-tau {{
    position: absolute;
    top: -3px;
    width: 2px;
    height: 14px;
    background-color: #0F1B2D;
}}
.aae-conf-value {{ font-size: 11.5px; color: #1E2A38; }}

/* -- Progress tracker -- */
.aae-progress {{ display: flex; align-items: flex-start; }}
.aae-progress-step {{ display: flex; flex-direction: column; align-items: center; flex: 1; position: relative; }}
.aae-progress-dot {{
    width: 24px;
    height: 24px;
    border-radius: 999px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 11px;
    font-weight: 700;
}}
.aae-progress-dot--done {{ background-color: #0D9488; color: #FFFFFF; }}
.aae-progress-dot--current {{ background-color: #F59E0B; color: #FFFFFF; }}
.aae-progress-dot--future {{ background-color: #FFFFFF; color: #9CA7B4; border: 2px solid #C7D0DB; }}
.aae-progress-label {{ font-size: 12px !important; font-weight: 700 !important; margin: 4px 0 0 0 !important; }}
.aae-progress-sub {{ font-size: 11.5px !important; color: #5B6878 !important; margin: 0 !important; }}
.aae-progress-connector {{
    position: absolute;
    top: 12px;
    left: 50%;
    width: 100%;
    height: 2px;
    background-color: #DDE3EA;
    z-index: -1;
}}
.aae-progress-connector--filled {{ background-color: #2DD4BF; }}

/* -- Main content: fill the page -- */
.block-container {{
    padding: 24px 28px !important;
    max-width: 100% !important;
}}
[data-testid="stVerticalBlock"] {{
    gap: 14px !important;
}}
[data-testid="stSidebar"] [data-testid="stVerticalBlock"] {{
    gap: 6px !important;
}}
</style>
"""


def widget_card_container(name: str):
    """A widget-bearing card's container: st.container(key="aae-card-
    <name>", border=False) -- the look (bg/border/radius/padding) comes
    entirely from CSS targeting [class*="st-key-aae-card"], not from
    Streamlit's own bordered-container chrome (which uses the version-
    pinned .st-emotion-cache-* class elsewhere in ui.py already fights
    with -- avoid stacking two different border mechanisms on one
    element)."""
    return st.container(key=f"aae-card-{name}", border=False)
