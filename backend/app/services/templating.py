"""Sandboxed message-template rendering (Phase 13).

Two `jinja2.sandbox.SandboxedEnvironment` instances back everything here:
one for delivery (`ChainableUndefined` -- a missing variable just renders as
empty, so a template referencing a field this notification happens not to
carry never blows up a live send) and one for preview
(`StrictUndefined` -- surfaces exactly where the template *would* break, for
the editor's live-preview panel). `SandboxedEnvironment` itself is what
blocks a template from reaching unsafe attributes (`''.__class__`, `self`,
etc.) -- see the "sandbox escape" tests in tests/test_templating.py.

`render()` is the delivery-time entry point (called from
`app/worker/outbox.py`'s `deliver()`): it never lets a broken template
block an alert from going out. Any failure -- a sandbox violation, a syntax
error that slipped past validation, a timeout, whatever -- falls back to
`APP_DEFAULT_TEMPLATES`, with `RenderOutcome.fallback_used=True` so the
worker can record *why* on the outbox row without failing the delivery.

`preview()` is the opposite: it's meant to show a template author exactly
what's wrong, so it never falls back -- a sandbox violation or undefined
variable there is reported, not hidden behind a default template.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from jinja2 import (
    ChainableUndefined,
    StrictUndefined,
    TemplateSyntaxError,
    Undefined,
    UndefinedError,
    meta,
)
from jinja2.sandbox import SandboxedEnvironment
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.base import AlertNotification, RenderedMessage
from app.models.template import MessageTemplate

logger = logging.getLogger(__name__)

RENDER_TIMEOUT_SECONDS = 2
MAX_RENDERED_BYTES = 256 * 1024

TEMPLATE_SLOTS = ("title", "body", "body_html")


# -- filters -----------------------------------------------------------------


def datetime_format(value: Any, fmt: str = "%Y-%m-%d %H:%M:%S %Z") -> str:
    """`{{ starts_at | datetime_format }}` / `{{ starts_at | datetime_format('%Y-%m-%d') }}`.

    Accepts a `datetime` (the normal case -- every timestamp field on
    `AlertNotification` is one) or an ISO 8601 string (in case a template
    author pipes a raw label/annotation value through it); anything else,
    or a missing/undefined value, renders as an empty string rather than
    raising -- a formatting filter must never be what breaks a render.
    """
    if value is None or isinstance(value, Undefined):
        return ""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    if not isinstance(value, datetime):
        return str(value)
    return value.strftime(fmt)


def humanize_duration(value: Any) -> str:
    """`{{ (ends_at - starts_at).total_seconds() | humanize_duration }}` ->
    e.g. "3분 20초". Accepts a number of seconds (int/float/numeric string);
    negative values clamp to 0. Missing/undefined/non-numeric input renders
    as an empty string, same reasoning as `datetime_format` above.
    """
    if value is None or isinstance(value, Undefined):
        return ""
    try:
        total = int(float(value))
    except (TypeError, ValueError):
        return ""
    total = max(total, 0)

    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)

    parts: list[str] = []
    if days:
        parts.append(f"{days}일")
    if hours:
        parts.append(f"{hours}시간")
    if minutes:
        parts.append(f"{minutes}분")
    if seconds or not parts:
        parts.append(f"{seconds}초")
    return " ".join(parts)


def strip_header_newlines(value: str) -> str:
    """Collapse embedded CR/LF to a space.

    The canonical implementation (moved here from `app/channels/email.py` in
    Phase 13 -- email.py now imports this instead of keeping its own copy):
    a rendered title is used as an email Subject (and potentially other
    header-like transports), and `email.message.Message.__setitem__` rejects
    any header value containing a raw newline not followed by whitespace --
    even a "technically valid" one is a header-injection vector (a second
    `\\r\\n` could start a new header). Every rendered title is sanitized
    here, regardless of which channel eventually uses it.
    """
    return value.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")


def _build_env(undefined_cls: type[Undefined], *, autoescape: bool) -> SandboxedEnvironment:
    env = SandboxedEnvironment(undefined=undefined_cls, autoescape=autoescape)
    env.filters["datetime_format"] = datetime_format
    env.filters["humanize_duration"] = humanize_duration
    return env


# Operational (delivery-time): missing variables render empty rather than
# raising -- see module docstring.
_OPERATIONAL_ENV = _build_env(ChainableUndefined, autoescape=False)
_OPERATIONAL_HTML_ENV = _build_env(ChainableUndefined, autoescape=True)

# Preview: StrictUndefined so an actual render attempt surfaces exactly
# where a template touches something undefined, instead of silently
# swallowing it -- see preview()'s use of it below.
_PREVIEW_ENV = _build_env(StrictUndefined, autoescape=False)
_PREVIEW_HTML_ENV = _build_env(StrictUndefined, autoescape=True)


# -- app-wide default template (used when a channel type declares none) -----

APP_DEFAULT_TEMPLATES: dict[str, str] = {
    "title": "[{{ trigger | upper }}] {{ alertname }}{% if severity %} ({{ severity }}){% endif %}",
    "body": (
        "Cluster: {{ cluster }}\n"
        "Namespace: {{ namespace or \"-\" }}\n"
        "Team: {{ team }}\n"
        "Starts At: {{ starts_at | datetime_format }}\n"
        "{% if ends_at %}Ends At: {{ ends_at | datetime_format }}\n{% endif %}"
        "{% if labels %}\nLabels:\n{% for key, value in labels.items() %}"
        "  {{ key }}: {{ value }}\n{% endfor %}{% endif %}"
        "{% if annotations %}\nAnnotations:\n{% for key, value in annotations.items() %}"
        "  {{ key }}: {{ value }}\n{% endfor %}{% endif %}"
        "{% if runbook_url %}\nRunbook: {{ runbook_url }}\n{% endif %}"
        "{% if grafana_url %}Grafana: {{ grafana_url }}\n{% endif %}"
        "{% if app_url %}View in app: {{ app_url }}\n{% endif %}"
    ),
}


# -- context builder ----------------------------------------------------------


def notification_context(n: AlertNotification) -> dict[str, Any]:
    """The variables a template source can reference. `now` is a zero-arg
    callable (`{{ now() }}`), not a fixed value, so a template gets the
    actual render-time timestamp rather than whenever this dict happened to
    be built.
    """
    return {
        "alertname": n.alertname,
        "severity": n.severity,
        "namespace": n.namespace,
        "cluster": n.cluster,
        "trigger": n.trigger,
        "labels": dict(n.labels),
        "annotations": dict(n.annotations),
        "starts_at": n.starts_at,
        "ends_at": n.ends_at,
        "team": n.team_slug,
        "app_url": n.app_url,
        "runbook_url": n.runbook_url,
        "grafana_url": n.grafana_url,
        "now": lambda: datetime.now(UTC),
    }


# -- rendering -----------------------------------------------------------------


def _compile_and_render(env: SandboxedEnvironment, source: str, context: dict[str, Any]) -> str:
    template = env.from_string(source)
    return template.render(**context)


async def _render_one(env: SandboxedEnvironment, source: str, context: dict[str, Any]) -> str:
    async with asyncio.timeout(RENDER_TIMEOUT_SECONDS):
        rendered = await asyncio.to_thread(_compile_and_render, env, source, context)

    encoded = rendered.encode("utf-8")
    if len(encoded) > MAX_RENDERED_BYTES:
        logger.warning(
            "rendered template output truncated at %d bytes (was %d)",
            MAX_RENDERED_BYTES,
            len(encoded),
        )
        rendered = encoded[:MAX_RENDERED_BYTES].decode("utf-8", errors="ignore")
    return rendered


async def _render_slots(
    template_strs: dict[str, str | None],
    n: AlertNotification,
    *,
    env: SandboxedEnvironment,
    html_env: SandboxedEnvironment,
) -> RenderedMessage:
    context = notification_context(n)

    title_source = template_strs.get("title") or ""
    body_source = template_strs.get("body") or ""
    body_html_source = template_strs.get("body_html")

    title = strip_header_newlines(await _render_one(env, title_source, context))
    body = await _render_one(env, body_source, context)
    body_html = (
        await _render_one(html_env, body_html_source, context) if body_html_source else None
    )
    return RenderedMessage(title=title, body=body, body_html=body_html)


@dataclass
class RenderOutcome:
    message: RenderedMessage
    fallback_used: bool
    # The failed render's exception class name, only set when fallback_used.
    error: str | None = None


async def render(template_strs: dict[str, str | None], n: AlertNotification) -> RenderOutcome:
    """Delivery-time render: `template_strs` is whatever
    `resolve_template()` returned (or a channel type's `default_templates`,
    or `APP_DEFAULT_TEMPLATES`, if that was `None`).

    Never raises -- any failure (sandbox violation, syntax error, timeout,
    oversized output) falls back to rendering `APP_DEFAULT_TEMPLATES`
    instead, so a broken custom template can never block delivery. The
    fallback render is not itself expected to fail (it's a fixed, tested
    template), but if it somehow does, that exception propagates -- there's
    no second fallback beneath the app default.
    """
    try:
        message = await _render_slots(
            template_strs, n, env=_OPERATIONAL_ENV, html_env=_OPERATIONAL_HTML_ENV
        )
        return RenderOutcome(message=message, fallback_used=False)
    except Exception as exc:  # noqa: BLE001 -- deliberately broad, see docstring.
        logger.warning(
            "template render failed (%s: %s) -- falling back to default template",
            type(exc).__name__,
            exc,
        )
        message = await _render_slots(
            APP_DEFAULT_TEMPLATES, n, env=_OPERATIONAL_ENV, html_env=_OPERATIONAL_HTML_ENV
        )
        return RenderOutcome(message=message, fallback_used=True, error=type(exc).__name__)


async def resolve_template(
    session: AsyncSession,
    rule_template_id: int | None,
    channel_template_id: int | None,
) -> dict[str, str | None] | None:
    """Priority: routing rule's own template, then the channel's, then
    `None` (meaning: caller should fall back to the channel type's
    `default_templates`, or `APP_DEFAULT_TEMPLATES` if that's empty too).

    A `template_id` pointing at a row that no longer exists (shouldn't
    happen -- both FKs are ON DELETE SET NULL -- but defensive regardless)
    is treated the same as no template_id at all, falling through to the
    next priority level rather than erroring.
    """
    for template_id in (rule_template_id, channel_template_id):
        if template_id is None:
            continue
        template = await session.get(MessageTemplate, template_id)
        if template is not None:
            return {
                "title": template.title_template,
                "body": template.body_template,
                "body_html": template.body_html_template,
            }
    return None


# -- validation ----------------------------------------------------------------


def validate_template_strings(template_strs: dict[str, str | None]) -> list[dict[str, Any]]:
    """Syntax-only validation (used by the create/update API, at save time):
    a `TemplateSyntaxError`'s line number, per slot. Doesn't catch sandbox
    violations or undefined variables -- those are only detectable by
    actually attempting a render (see `preview()`), which needs a concrete
    `AlertNotification` context this function deliberately doesn't require.
    """
    errors: list[dict[str, Any]] = []
    for slot in TEMPLATE_SLOTS:
        source = template_strs.get(slot)
        if not source:
            continue
        try:
            _PREVIEW_ENV.parse(source)
        except TemplateSyntaxError as exc:
            errors.append(
                {"slot": slot, "lineno": exc.lineno, "message": exc.message or str(exc)}
            )
    return errors


_CONTEXT_VARIABLE_NAMES = {
    "alertname",
    "severity",
    "namespace",
    "cluster",
    "trigger",
    "labels",
    "annotations",
    "starts_at",
    "ends_at",
    "team",
    "app_url",
    "runbook_url",
    "grafana_url",
    "now",
}


def _undeclared_variables(slot: str, source: str) -> set[str]:
    try:
        ast = _PREVIEW_ENV.parse(source)
    except TemplateSyntaxError:
        return set()
    return meta.find_undeclared_variables(ast) - _CONTEXT_VARIABLE_NAMES


async def preview(
    template_strs: dict[str, str | None], n: AlertNotification
) -> dict[str, Any]:
    """The editor's live-preview endpoint.

    Returns `{"rendered": {...} | None, "errors": [...], "warnings": [...]}`:
    - `warnings`: variable names referenced by any slot that aren't part of
      the known context (`notification_context`'s keys) -- found via static
      AST analysis (`jinja2.meta.find_undeclared_variables`), so a template
      using an undefined variable is flagged without that alone blocking
      the preview (an undefined variable is a *maybe-mistake*, not
      necessarily a broken template -- it could be a typo, or a slot that's
      fine rendering empty for this particular sample).
    - `errors`: syntax errors (with line numbers) if any slot fails to
      parse, or -- if parsing succeeds but rendering raises for a reason
      *other* than an undefined variable (a sandbox violation, a timeout,
      ...) -- a single entry describing that failure. `rendered` is `None`
      whenever `errors` is non-empty.

    A `_PREVIEW_ENV` (`StrictUndefined`) pass runs first specifically to
    surface those non-undefined problems -- sandbox violations in
    particular must be reported as real errors here, not silently
    tolerated the way `render()`'s fallback does at delivery time. Its
    `UndefinedError`s are expected and swallowed (undefined variables are
    already fully enumerated via the static analysis above); the actual
    `rendered` output for display always comes from a second, lenient pass
    (`_OPERATIONAL_ENV`/`ChainableUndefined`) so a merely-undefined variable
    never leaves the preview panel blank.
    """
    errors = validate_template_strings(template_strs)
    warnings = sorted(
        {
            var
            for slot in TEMPLATE_SLOTS
            for var in _undeclared_variables(slot, template_strs.get(slot) or "")
        }
    )
    if errors:
        return {"rendered": None, "errors": errors, "warnings": warnings}

    try:
        await _render_slots(template_strs, n, env=_PREVIEW_ENV, html_env=_PREVIEW_HTML_ENV)
    except UndefinedError:
        pass  # Expected -- already reflected in `warnings` above.
    except Exception as exc:  # noqa: BLE001 -- sandbox SecurityError, TimeoutError, etc.
        return {
            "rendered": None,
            "errors": [{"slot": None, "lineno": None, "message": f"{type(exc).__name__}: {exc}"}],
            "warnings": warnings,
        }

    try:
        message = await _render_slots(
            template_strs, n, env=_OPERATIONAL_ENV, html_env=_OPERATIONAL_HTML_ENV
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "rendered": None,
            "errors": [{"slot": None, "lineno": None, "message": f"{type(exc).__name__}: {exc}"}],
            "warnings": warnings,
        }

    return {"rendered": message.model_dump(), "errors": [], "warnings": warnings}


# -- variable reference ---------------------------------------------------------

# AlertNotification field name -> the (differently-named) context key
# notification_context() actually exposes it under.
_FIELD_TO_CONTEXT_NAME = {"team_slug": "team"}
# Fields that exist on AlertNotification but aren't part of the template
# context (internal plumbing, not something a template author would render).
_NON_CONTEXT_FIELDS = {"event_id"}

_VARIABLE_DESCRIPTIONS: dict[str, str] = {
    "alertname": "알럿명",
    "severity": "심각도 (critical/warning/info/none일 수 있음)",
    "namespace": "네임스페이스",
    "cluster": "클러스터명",
    "trigger": "트리거 (firing/resolved/test)",
    "labels": "알럿 레이블 dict -- labels.<key> 형태로 개별 값 참조",
    "annotations": "알럿 어노테이션 dict -- annotations.<key> 형태로 개별 값 참조",
    "starts_at": "시작 시각 (datetime_format 필터 권장)",
    "ends_at": "종료 시각 -- resolved 트리거일 때만 값이 있음",
    "team": "팀 슬러그",
    "app_url": "이 알럿의 앱 상세 페이지 링크",
    "runbook_url": "런북 링크 (없을 수 있음)",
    "grafana_url": "Grafana 대시보드 링크 (없을 수 있음)",
}


def template_variables() -> list[dict[str, str]]:
    """The editor's variable-reference sidebar: auto-generated from
    `AlertNotification`'s own fields (so a field added/removed there is
    reflected here without a separate hand-maintained list going stale),
    plus a few synthetic entries (`labels.*`, `annotations.*`, `now()`) that
    aren't top-level fields themselves.
    """
    variables: list[dict[str, str]] = []
    for field_name in AlertNotification.model_fields:
        if field_name in _NON_CONTEXT_FIELDS:
            continue
        name = _FIELD_TO_CONTEXT_NAME.get(field_name, field_name)
        variables.append(
            {
                "name": name,
                "description": _VARIABLE_DESCRIPTIONS.get(name, name),
                "example": f"{{{{ {name} }}}}",
            }
        )
    variables.extend(
        [
            {
                "name": "labels.<key>",
                "description": "레이블 개별 값 (예: labels.pod)",
                "example": "{{ labels.pod }}",
            },
            {
                "name": "annotations.<key>",
                "description": "어노테이션 개별 값 (예: annotations.summary)",
                "example": "{{ annotations.summary }}",
            },
            {
                "name": "now()",
                "description": "현재 시각(UTC)을 반환하는 호출 가능 함수",
                "example": "{{ now() | datetime_format }}",
            },
        ]
    )
    return variables
