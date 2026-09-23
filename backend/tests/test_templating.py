"""Tests for app/services/templating.py: sandboxed rendering, fallback,
resolve priority, preview, filters, and the variable reference list.

No live DB/app fixture needed for most of these -- `render()`/`preview()`
are pure functions of (template strings, AlertNotification). `resolve_template`
is the one exception (it hits the DB), so it uses the `app` fixture from
conftest.py the same way other service-layer tests do.
"""

from datetime import UTC, datetime

import pytest

import app.db as db_module
from app.channels.base import AlertNotification
from app.models.team import Team
from app.models.template import MessageTemplate
from app.services.templating import (
    APP_DEFAULT_TEMPLATES,
    datetime_format,
    humanize_duration,
    preview,
    render,
    resolve_template,
    strip_header_newlines,
    template_variables,
    validate_template_strings,
)


def _notification(**overrides) -> AlertNotification:
    base = AlertNotification.example().model_dump()
    base.update(overrides)
    return AlertNotification(**base)


# -- sandbox -------------------------------------------------------------------


async def test_sandbox_blocks_class_escape_gadget_and_falls_back() -> None:
    """A classic SSTI RCE gadget chain (walk from a literal to `object`'s
    subclasses to reach something like `os`) must never actually execute --
    SandboxedEnvironment blocks the dunder attribute chain, and render()
    falls back to the default template rather than propagating the error.
    """
    n = _notification()
    outcome = await render(
        {"title": "ok", "body": "{{ ().__class__.__bases__[0].__subclasses__() }}"}, n
    )
    assert outcome.fallback_used is True
    assert outcome.error == "SecurityError"
    # The fallback actually rendered the default template, not an empty/broken body.
    assert n.alertname in outcome.message.body


async def test_sandbox_blocks_self_globals_gadget() -> None:
    """`self.__init__.__globals__` is the other classic Jinja SSTI entry
    point (reaching the rendering module's globals, then `os`/`config`).
    Whether or not this specific chain trips render()'s fallback (a merely
    *printed* unsafe attribute can safely resolve to an empty string under
    the lenient delivery-time undefined -- see test_templating.py's sibling
    assertion below), the one invariant that must hold is: nothing about
    the real Python object (`__globals__`'s contents, `os`, config values,
    ...) ever appears in the rendered output.
    """
    n = _notification()
    outcome = await render({"title": "ok", "body": "{{ self.__init__.__globals__ }}"}, n)
    assert "os" not in outcome.message.body
    assert "secret" not in outcome.message.body.lower()
    assert "{'" not in outcome.message.body  # no dict repr of real globals leaked


async def test_sandbox_blocks_dunder_class_access_in_preview() -> None:
    """Unlike render()'s lenient delivery-time environment, preview() uses a
    StrictUndefined pass specifically so a sandbox violation is surfaced as
    an actual error to the template author, not silently swallowed.
    """
    n = _notification()
    result = await preview({"title": "{{ ''.__class__ }}", "body": "ok"}, n)
    assert result["rendered"] is None
    assert result["errors"]
    assert "SecurityError" in result["errors"][0]["message"]


async def test_sandbox_allows_cycler_and_joiner() -> None:
    """`cycler`/`joiner` are Jinja2's own intentionally-exposed globals (not
    attribute-access based), unaffected by the sandbox -- a legitimate
    template using them must render normally, not be treated as an escape
    attempt.
    """
    n = _notification()
    outcome = await render(
        {
            "title": "{% set c = cycler('a', 'b') %}{{ c.next() }}{{ c.next() }}",
            "body": "{% set j = joiner(', ') %}{{ j() }}x{{ j() }}y",
        },
        n,
    )
    assert outcome.fallback_used is False
    assert outcome.message.title == "ab"
    assert outcome.message.body == "x, y"


# -- render: ChainableUndefined, timeout, cap, title newline strip -------------


async def test_missing_variable_chain_renders_empty_not_an_error() -> None:
    """ChainableUndefined (the operational/delivery undefined class) lets a
    deep attribute chain off a genuinely undefined top-level name resolve
    to an empty string instead of raising -- a template author's typo or a
    field this notification doesn't carry must not break delivery.
    """
    n = _notification()
    outcome = await render(
        {"title": "prefix-{{ nonexistent.chain.deeper }}-suffix", "body": "ok"}, n
    )
    assert outcome.fallback_used is False
    assert outcome.message.title == "prefix--suffix"


async def test_missing_label_key_renders_empty() -> None:
    n = _notification(labels={"alertname": "X"})
    outcome = await render({"title": "{{ labels.not_a_real_key }}", "body": "ok"}, n)
    assert outcome.fallback_used is False
    assert outcome.message.title == ""


async def test_render_timeout_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """A slow render must not hang delivery -- render() times out and falls
    back rather than blocking the outbox worker.

    `_compile_and_render` (the sync function `_render_one` runs via
    `asyncio.to_thread`) is monkeypatched to sleep past a shrunk timeout,
    rather than actually running a runaway Jinja loop: `asyncio.timeout`
    only cancels *awaiting* the thread-pool future, it can't actually kill
    the OS thread mid-CPU-loop, so a genuinely unbounded loop here would
    keep burning CPU in the background for the rest of the test run instead
    of safely finishing on its own shortly after (as a bounded `sleep`
    does).
    """
    import time

    import app.services.templating as templating_module

    monkeypatch.setattr(templating_module, "RENDER_TIMEOUT_SECONDS", 0.05)

    real_compile_and_render = templating_module._compile_and_render
    slow_source = "irrelevant -- render is stubbed"

    def _slow_for_the_custom_body_only(env, source, context):
        # Only the custom template's body slot is slowed down -- the
        # fallback render (APP_DEFAULT_TEMPLATES, a different source string)
        # must still complete normally within the shrunk timeout, exactly
        # like a real broken/slow custom template's fallback would.
        if source == slow_source:
            time.sleep(0.2)
            return ""
        return real_compile_and_render(env, source, context)

    monkeypatch.setattr(
        templating_module, "_compile_and_render", _slow_for_the_custom_body_only
    )

    n = _notification()
    outcome = await render({"title": "ok", "body": slow_source}, n)
    assert outcome.fallback_used is True
    assert outcome.error == "TimeoutError"


async def test_render_output_is_capped_at_256kb() -> None:
    n = _notification()
    outcome = await render({"title": "ok", "body": "{{ 'x' * 300000 }}"}, n)
    assert outcome.fallback_used is False
    assert len(outcome.message.body.encode("utf-8")) <= 256 * 1024


async def test_title_embedded_newlines_are_stripped() -> None:
    n = _notification()
    outcome = await render({"title": "line1\r\nline2\nline3", "body": "ok"}, n)
    assert "\n" not in outcome.message.title
    assert "\r" not in outcome.message.title
    assert outcome.message.title == "line1 line2 line3"


def test_strip_header_newlines() -> None:
    assert strip_header_newlines("a\r\nb\rc\nd") == "a b c d"


# -- filters ---------------------------------------------------------------


def test_datetime_format_filter() -> None:
    value = datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)
    assert datetime_format(value, "%Y-%m-%d") == "2026-03-04"
    assert datetime_format(None) == ""


def test_humanize_duration_filter() -> None:
    assert humanize_duration(200) == "3분 20초"
    assert humanize_duration(0) == "0초"
    assert humanize_duration(90061) == "1일 1시간 1분 1초"
    assert humanize_duration(None) == ""
    assert humanize_duration(-5) == "0초"


async def test_filters_available_in_templates() -> None:
    n = _notification(starts_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC))
    outcome = await render(
        {
            "title": "{{ starts_at | datetime_format('%Y-%m-%d') }}",
            "body": "{{ 200 | humanize_duration }}",
        },
        n,
    )
    assert outcome.fallback_used is False
    assert outcome.message.title == "2026-01-02"
    assert outcome.message.body == "3분 20초"


# -- fallback (broken stored template) -----------------------------------------


async def test_broken_syntax_falls_back_to_app_default() -> None:
    n = _notification(alertname="BrokenTemplateAlert")
    outcome = await render({"title": "{{ unterminated", "body": "ok"}, n)
    assert outcome.fallback_used is True
    assert outcome.error == "TemplateSyntaxError"
    assert "BrokenTemplateAlert" in outcome.message.title  # rendered via APP_DEFAULT_TEMPLATES


async def test_healthy_template_does_not_fall_back() -> None:
    n = _notification(alertname="HealthyAlert")
    outcome = await render({"title": "{{ alertname }}", "body": "ok"}, n)
    assert outcome.fallback_used is False
    assert outcome.error is None
    assert outcome.message.title == "HealthyAlert"


# -- resolve_template priority --------------------------------------------------


async def _make_team_and_templates(session) -> tuple[Team, MessageTemplate, MessageTemplate]:
    team = Team(slug="rt-team", name="RT Team")
    session.add(team)
    await session.flush()

    rule_template = MessageTemplate(
        team_id=team.id,
        name="rule-template",
        title_template="RULE: {{ alertname }}",
        body_template="rule body",
    )
    channel_template = MessageTemplate(
        team_id=team.id,
        name="channel-template",
        title_template="CHANNEL: {{ alertname }}",
        body_template="channel body",
    )
    session.add_all([rule_template, channel_template])
    await session.flush()
    return team, rule_template, channel_template


async def test_resolve_template_prefers_rule_over_channel(app) -> None:
    async with db_module.async_session_factory() as session:
        _team, rule_template, channel_template = await _make_team_and_templates(session)
        await session.commit()

        resolved = await resolve_template(session, rule_template.id, channel_template.id)
        assert resolved is not None
        assert resolved["title"] == "RULE: {{ alertname }}"


async def test_resolve_template_falls_back_to_channel_when_no_rule_template(app) -> None:
    async with db_module.async_session_factory() as session:
        _team, _rule_template, channel_template = await _make_team_and_templates(session)
        await session.commit()

        resolved = await resolve_template(session, None, channel_template.id)
        assert resolved is not None
        assert resolved["title"] == "CHANNEL: {{ alertname }}"


async def test_resolve_template_returns_none_when_neither_set(app) -> None:
    async with db_module.async_session_factory() as session:
        resolved = await resolve_template(session, None, None)
        assert resolved is None


async def test_resolve_template_ignores_dangling_id(app) -> None:
    """A template_id pointing at a row that no longer exists (shouldn't
    happen given the ON DELETE SET NULL FKs, but defensive regardless)
    falls through to the next priority level instead of erroring.
    """
    async with db_module.async_session_factory() as session:
        _team, _rule_template, channel_template = await _make_team_and_templates(session)
        await session.commit()

        resolved = await resolve_template(session, 999999, channel_template.id)
        assert resolved is not None
        assert resolved["title"] == "CHANNEL: {{ alertname }}"


# -- validate_template_strings / preview ---------------------------------------


def test_validate_template_strings_reports_syntax_error_with_lineno() -> None:
    errors = validate_template_strings({"title": "ok", "body": "line1\n{% if x %}"})
    assert len(errors) == 1
    assert errors[0]["slot"] == "body"
    assert errors[0]["lineno"] == 2


def test_validate_template_strings_no_errors_for_valid_templates() -> None:
    errors = validate_template_strings(
        {"title": "{{ alertname }}", "body": "{{ cluster }}", "body_html": "<b>{{ cluster }}</b>"}
    )
    assert errors == []


async def test_preview_reports_undefined_variable_as_warning_but_still_renders() -> None:
    n = _notification(alertname="X")
    result = await preview({"title": "{{ alertname }}: {{ not_a_thing }}", "body": "ok"}, n)
    assert result["errors"] == []
    assert result["warnings"] == ["not_a_thing"]
    assert result["rendered"]["title"] == "X: "


async def test_preview_sample_alert_when_no_event_given() -> None:
    result = await preview({"title": "{{ alertname }}", "body": "ok"}, AlertNotification.example())
    assert result["rendered"]["title"] == "KamTestAlert"


def test_template_variables_includes_core_fields_and_synthetic_entries() -> None:
    variables = template_variables()
    names = {v["name"] for v in variables}
    assert {"alertname", "severity", "cluster", "trigger", "team", "app_url"} <= names
    assert "labels.<key>" in names
    assert "annotations.<key>" in names
    assert "now()" in names
    # event_id is internal plumbing, not a template-facing variable.
    assert "event_id" not in names


def test_app_default_templates_render_without_a_custom_template() -> None:
    assert set(APP_DEFAULT_TEMPLATES) == {"title", "body"}
