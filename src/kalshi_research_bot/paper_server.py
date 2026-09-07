from __future__ import annotations

import base64
import gzip
import html
import json
import os
import secrets
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from math import isfinite
from pathlib import Path
from typing import Mapping
from urllib.parse import parse_qs, urlparse

from .auth import (
    AuthPrincipal,
    CSRF_COOKIE_NAME,
    LocalAuthStore,
    ROLES,
    SESSION_COOKIE_NAME,
    csrf_token_from_cookie,
    role_allows,
    session_token_from_cookie,
    user_auth_enabled,
)
from .combo_safety import slip_has_authoritative_combo_evidence
from .dashboard_assets import (
    LOGIN_SCRIPT,
    OPS_SCRIPT,
    SCRIPT,
    STYLESHEET,
    lookup as lookup_asset,
)
from .database import database_startup_status, json_default, production_safety_status
from .evaluation.model_validation import wilson_interval
from .connectors.http import prune_http_cache
from .config import repo_path
from .monitoring import build_internal_status
from .operator_inbox import OperatorInbox, PRIORITIES, TARGETS
from .review_packet import (
    SLIP_SOURCES,
    build_all_review_packets,
    build_review_packet,
    render_review_packet_text,
    safe_review_packet_filename,
)
from .research_record import build_research_record
from .slip_report import build_slip_analysis
from .slip_safety import consumer_payload, gate_slip_payload, slip_payload_gate
from .source_quality import build_dashboard_quality_gate
from .source_data_store import SourceDataStore
from .business_store import create_store
from .kalshi_ingestion import load_latest_kalshi_snapshot
from .sports_board import load_sports_board, summarize_sports_board
from .sports_clv import build_sports_clv_report
from .storage import PostgresStore


REFRESH_COOLDOWN_SECONDS = 60
DEFAULT_KALSHI_RUN_ID = "stage3a_20260703_170707"
DEFAULT_REFRESH_LEDGER_MAX_PAYLOAD_AGE_SECONDS = 1800
DEFAULT_DASHBOARD_MAX_SLIP_AGE_SECONDS = 1800
DEFAULT_DETAIL_PAGE_SIZE = 50
# The stake the slip card's arithmetic is quoted against. Matches the "Est. $5
# Payout" figure already on the card, so the two cannot disagree.
DEFAULT_SLIP_STAKE_DOLLARS = 5.0
MAX_DETAIL_PAGE_SIZE = 200
HOSTED_RUNTIME_ENV_KEYS = (
    "RAILWAY_ENVIRONMENT",
    "RAILWAY_ENVIRONMENT_ID",
    "RAILWAY_PROJECT_ID",
    "RAILWAY_PUBLIC_DOMAIN",
)
REFRESH_ACTION_HEADER = "X-Research-Action"
REFRESH_ACTION_VALUE = "refresh-dashboard"
# Queueing an operator message is a state change, so it needs the same custom
# header. A browser cannot attach a custom header to a cross-origin request
# without a preflight, and this server sends no CORS headers, so requiring one
# is what makes the request unforgeable from another site.
OPERATOR_ACTION_VALUE = "queue-operator-message"
SOURCE_REFRESH_ACTION_VALUE = "queue-source-refresh"


def _env_flag(values: Mapping[str, str], name: str, default: bool = False) -> bool:
    value = values.get(name)
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def hosted_runtime(env: Mapping[str, str] | None = None) -> bool:
    values = os.environ if env is None else env
    return any(str(values.get(name) or "").strip() for name in HOSTED_RUNTIME_ENV_KEYS) or str(
        values.get("APP_ENV") or ""
    ).strip().lower() in {"staging", "production"}


def dashboard_auth_enabled(env: dict[str, str] | None = None) -> bool:
    values = os.environ if env is None else env
    require_hosted_auth = _env_flag(values, "DASHBOARD_REQUIRE_AUTH_WHEN_HOSTED", True)
    return (
        _env_flag(values, "DASHBOARD_AUTH_ENABLED")
        or user_auth_enabled(values)
        or bool(values.get("DASHBOARD_AUTH_PASSWORD"))
        or (require_hosted_auth and hosted_runtime(values))
    )


def dashboard_auth_configured(env: Mapping[str, str] | None = None) -> bool:
    values = os.environ if env is None else env
    basic_configured = _env_flag(values, "DASHBOARD_BASIC_FALLBACK_ENABLED", True) and bool(
        values.get("DASHBOARD_AUTH_PASSWORD")
    )
    return basic_configured or user_auth_enabled(values) or not dashboard_auth_enabled(dict(values))


def valid_dashboard_auth(header: str | None, env: dict[str, str] | None = None) -> bool:
    values = os.environ if env is None else env
    if not dashboard_auth_enabled(values):
        return True
    expected_password = values.get("DASHBOARD_AUTH_PASSWORD")
    if not expected_password:
        return False
    expected_username = values.get("DASHBOARD_AUTH_USERNAME", "hawknetic")
    if not header or not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header.removeprefix("Basic "), validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False
    username, separator, password = decoded.partition(":")
    if not separator:
        return False
    return secrets.compare_digest(username, expected_username) and secrets.compare_digest(password, expected_password)


def basic_auth_role(env: Mapping[str, str]) -> str:
    """The role shared basic-auth credentials grant.

    An explicit `DASHBOARD_BASIC_AUTH_ROLE` always wins. Anything else is
    `read_only`, so privilege is something a deployment asks for rather than
    something it receives for staying silent.

    This default was previously conditional: without user accounts the password
    was treated as the instance's only identity, so its holder was assumed to be
    the owner and given admin. The argument is a fair one, and it fails on the
    deployment that matters. The production service has no user accounts and no
    `DASHBOARD_BASIC_AUTH_ROLE`, which is exactly the branch that returns admin --
    so the customer/operator split stays inert precisely where it was introduced
    to apply, and the credential most likely to be shared is the one carrying the
    most privilege. An owner who wants their controls back sets one variable, and
    the failure mode of forgetting is a missing button rather than a stranger
    holding the operator panel.

    An unrecognised value is a typo, not a request, and takes the floor -- a
    misspelling used to land on `read_only` while saying nothing at all landed on
    `admin`, which had the two mistakes backwards.
    """
    requested = str(env.get("DASHBOARD_BASIC_AUTH_ROLE") or "").strip().lower()
    if requested:
        return requested if requested in set(ROLES) else "read_only"
    return "read_only"


def authenticate_dashboard_request(
    authorization_header: str | None,
    cookie_header: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
    auth_store: LocalAuthStore | None = None,
) -> AuthPrincipal | None:
    values = os.environ if env is None else env
    if not dashboard_auth_enabled(dict(values)):
        # Running with auth off is a local convenience: it is your own machine,
        # so the full operator view is what you want. On a hosted runtime the
        # same state means anyone who finds the URL is unauthenticated, and
        # handing them admin would expose every operator route to the public.
        # Auth-off deployments stay reachable -- a staging box with no login
        # still works -- they just get the reader view.
        unprotected_role = "read_only" if hosted_runtime(values) else "admin"
        return AuthPrincipal(username="local", role=unprotected_role, auth_method="local_unprotected")
    if user_auth_enabled(values) and auth_store is not None:
        session_token = session_token_from_cookie(cookie_header)
        principal = auth_store.resolve_session(session_token or "")
        if principal is not None:
            return principal
    basic_fallback_enabled = _env_flag(values, "DASHBOARD_BASIC_FALLBACK_ENABLED", True)
    if basic_fallback_enabled and valid_dashboard_auth(authorization_header, dict(values)):
        return AuthPrincipal(
            username=str(values.get("DASHBOARD_AUTH_USERNAME") or "hawknetic"),
            role=basic_auth_role(values),
            auth_method="basic_fallback",
        )
    return None


def build_session_cookie(session_token: str, *, secure: bool) -> str:
    parts = [
        f"{SESSION_COOKIE_NAME}={session_token}",
        "Path=/",
        "HttpOnly",
        "SameSite=Strict",
        "Max-Age=28800",
    ]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def clear_session_cookie(*, secure: bool) -> str:
    parts = [
        f"{SESSION_COOKIE_NAME}=",
        "Path=/",
        "HttpOnly",
        "SameSite=Strict",
        "Max-Age=0",
    ]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def build_csrf_cookie(csrf_token: str, *, secure: bool) -> str:
    """Carry the CSRF token in a cookie the page's own script can read.

    The session cookie stays HttpOnly; this one deliberately does not, because
    the browser is the only party that can hand the token back in a header. It
    survives new tabs and restarts, which a per-tab store does not, and
    SameSite=Strict keeps a cross-site page from causing it to be sent at all.
    """
    parts = [
        f"{CSRF_COOKIE_NAME}={csrf_token}",
        "Path=/",
        "SameSite=Strict",
        "Max-Age=28800",
    ]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def clear_csrf_cookie(*, secure: bool) -> str:
    parts = [
        f"{CSRF_COOKIE_NAME}=",
        "Path=/",
        "SameSite=Strict",
        "Max-Age=0",
    ]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def render_login_page() -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  {FAVICON_LINK}
  <meta name="theme-color" content="#080f14">
  <title>Sign in · Hawknetic Predictions</title>
  <link rel="stylesheet" href="{STYLESHEET.url}">
</head>
<body class="login-page">
  <div class="login-shell">
    <section class="login-story" aria-labelledby="login-story-title">
      {render_brand(href="/")}
      <h1 id="login-story-title">Review the data.<br><span>Make your own call.</span></h1>
      <p>A private research workspace for fresh Kalshi source evidence, exact listed combo verification, and transparent review packets.</p>
      <ul class="feature-list">
        <li><b>&#8635;</b><span><strong>Fresh source evidence</strong><small>Timestamped public market snapshots with stale-data blocking.</small></span></li>
        <li><b>&#9671;</b><span><strong>Exact contract validation</strong><small>Only verified listed combinations enter the review workflow.</small></span></li>
        <li><b>&#9636;</b><span><strong>Transparent review packets</strong><small>See every ticker, side, price, event time, and probability source.</small></span></li>
        <li><b>&#10003;</b><span><strong>Research-only controls</strong><small>No automatic trading, order upload, or guaranteed outcomes.</small></span></li>
      </ul>
      <div class="research-trust"><span><i></i>Private workspace</span><span><i></i>Freshness gated</span><span><i></i>Manual review only</span></div>
    </section>
    <main class="login-card">
      <p class="form-kicker">Private research platform</p>
      <h2>Welcome back</h2>
      <p>Sign in with your research account to open the live builder.</p>
      <form id="login-form">
        <label>Username<input name="username" autocomplete="username" placeholder="Enter your username" required></label>
        <label>Password<input name="password" type="password" autocomplete="current-password" placeholder="Enter your password" required></label>
        <button class="btn btn-primary" type="submit">Sign in to Hawknetic Predictions &#8594;</button>
        <p id="login-status" role="status" aria-live="polite"></p>
      </form>
      <div class="login-boundary">Research and decision support only. Your account cannot place or upload orders.</div>
    </main>
  </div>
  <script src="{LOGIN_SCRIPT.url}" defer></script>
</body>
</html>"""


def render_operator_page() -> str:
    priority_options = "".join(
        f'<option value="{value}"{" selected" if value == "normal" else ""}>{value.title()}</option>'
        for value in PRIORITIES
    )
    target_options = "".join(f'<option value="{value}">{value.title()}</option>' for value in TARGETS)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  {FAVICON_LINK}
  <meta name="theme-color" content="#080f14">
  <title>Private Operator Inbox</title>
  <link rel="stylesheet" href="{STYLESHEET.url}">
</head>
<body>
  <header class="app-topbar">{render_brand(href="/")}</header>
  <main class="ops-shell">
    <div>
      <span class="section-label">Manual review</span>
      <h1>Private operator inbox</h1>
    </div>
    <div class="alert is-warning">
      <span class="alert-icon" aria-hidden="true">!</span>
      <div><strong>Manual review only</strong><p>Messages placed here are stored as instructions. They never run commands, edit code, deploy, trade, or contact an account automatically.</p></div>
    </div>
    <form class="ops-form" id="operator-form">
      <label>Title<input name="title" maxlength="200" required></label>
      <label>Priority<select name="priority">{priority_options}</select></label>
      <label>Target<select name="target">{target_options}</select></label>
      <label>Message for the operator<textarea name="body" maxlength="100000" required></textarea></label>
      <button class="btn btn-primary" type="submit">Queue for review</button>
      <div id="form-status" class="status" role="status" aria-live="polite"></div>
    </form>
    <section id="queue" aria-label="Queued operator messages"></section>
    <p><a href="/">&#8592; Back to research dashboard</a></p>
  </main>
  <script src="{OPS_SCRIPT.url}" defer></script>
</body>
</html>"""


def valid_research_action(headers: Mapping[str, str] | None, expected: str) -> bool:
    """Whether the request carries the custom action header for this operation.

    Session CSRF tokens do not cover Basic-authenticated principals -- there is
    no session to bind a token to, so ``valid_session_csrf`` returns True for
    them. Browsers cache Basic credentials and send them on cross-origin
    requests, so a state-changing endpoint that relies on the session token
    alone is forgeable whenever the body can be sent as a *simple* request
    (``text/plain``, ``application/x-www-form-urlencoded``, ``multipart/form-data``),
    which is exactly the set of content types that skip the CORS preflight.

    A custom header cannot be attached cross-origin without a preflight, and
    this server sends no CORS headers, so the preflight fails and the forged
    request never arrives.
    """

    if headers is None:
        return False
    value = str(headers.get(REFRESH_ACTION_HEADER) or "")
    return secrets.compare_digest(value, expected)


def valid_refresh_action(headers: Mapping[str, str] | None) -> bool:
    return valid_research_action(headers, REFRESH_ACTION_VALUE)


def valid_json_content_type(headers: Mapping[str, str] | None) -> bool:
    """Whether the body is declared as JSON.

    Defence in depth beside the action header: the three content types a
    cross-site form can send without a preflight are precisely the ones this
    rejects, so a forged post fails here even if the header check were ever
    relaxed.
    """

    if headers is None:
        return False
    declared = str(headers.get("Content-Type") or "").split(";")[0].strip().lower()
    return declared == "application/json"


def dashboard_security_headers() -> dict[str, str]:
    return {
        "Content-Security-Policy": (
            "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
            "form-action 'self'; img-src 'self' data:; connect-src 'self'; "
            "script-src 'self'; style-src 'self'; font-src 'self'"
        ),
        "Cross-Origin-Opener-Policy": "same-origin",
        "Cross-Origin-Resource-Policy": "same-origin",
        "Permissions-Policy": "camera=(), geolocation=(), microphone=()",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
    }


def append_jsonl(path: Path, payload: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
    except OSError:
        return


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def safe_dashboard_payload(payload: dict) -> dict:
    return gate_slip_payload(
        payload,
        max_age_seconds=_env_int(
            "DASHBOARD_MAX_SLIP_AGE_SECONDS",
            DEFAULT_DASHBOARD_MAX_SLIP_AGE_SECONDS,
        ),
    )


def build_detail_collection_payload(
    payload: dict,
    collection: str,
    query: Mapping[str, list[str]] | None = None,
) -> dict:
    """Return one bounded, freshness-gated collection for authenticated clients."""
    if collection not in {"games", "markets"}:
        raise ValueError(f"unsupported_detail_collection:{collection}")
    query = query or {}

    def bounded_integer(name: str, default: int, maximum: int) -> int:
        raw = (query.get(name) or [str(default)])[0]
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = default
        return max(0, min(value, maximum))

    records = list(payload.get(collection) or [])
    total_count = len(records)
    gate = payload.get("public_data_gate") or {}
    ready = gate.get("status") == "ready"
    offset = bounded_integer("offset", 0, total_count)
    limit = bounded_integer("limit", DEFAULT_DETAIL_PAGE_SIZE, MAX_DETAIL_PAGE_SIZE)
    visible = records[offset : offset + limit] if ready and limit else []
    next_offset = offset + len(visible)
    if next_offset >= total_count or not ready:
        next_offset = None
    return {
        "collection": collection,
        "generated_at": payload.get("generated_at"),
        "public_data_gate": gate,
        "status": "ready" if ready else "withheld",
        "total_count": total_count,
        "returned_count": len(visible),
        "withheld_count": 0 if ready else total_count,
        "offset": offset,
        "limit": limit,
        "next_offset": next_offset,
        "items": visible,
    }


def build_data_catalog_payload(payload: dict, *, viewer_role: str = "read_only") -> dict:
    """Describe the data routes this viewer may actually fetch.

    The catalog is a directory, so listing a route the caller will be refused is
    worse than omitting it: it advertises data as available and then answers 403.
    Each entry is therefore filtered by the same role its endpoint demands, and
    the default is the least privilege -- a caller that forgets to say who it is
    should be shown less, not everything.
    """
    gate = payload.get("public_data_gate") or {}
    ready = gate.get("status") == "ready"
    collections = {
        "games": {
            "count": len(payload.get("games") or []),
            "href": "/api/v1/games?limit=50&offset=0",
        },
        "markets": {
            "count": len(payload.get("markets") or []),
            "href": "/api/v1/markets?limit=50&offset=0",
        },
    }
    if role_allows(viewer_role, "researcher"):
        collections["slip_analysis"] = {"href": "/slip-analysis.json"}
    if role_allows(viewer_role, "admin"):
        collections.update(
            {
                "sports": {"href": "/sports.json?detail=full"},
                "sports_clv": {"href": "/sports-clv.json"},
                "source_data": {"href": "/api/v1/source-data"},
                "source_entities": {"href": "/api/v1/source-data/entities?limit=50&offset=0"},
                "external_markets": {"href": "/api/v1/source-data/markets?limit=50&offset=0"},
                "live_player_data": {"href": "/api/v1/source-data/live?limit=50"},
            }
        )
    return {
        "generated_at": payload.get("generated_at"),
        "status": "ready" if ready else "withheld",
        "public_data_gate": gate,
        "collections": collections,
    }


def _query_value(query: Mapping[str, list[str]], name: str) -> str | None:
    value = str((query.get(name) or [""])[0]).strip()
    return value or None


def _query_integer(
    query: Mapping[str, list[str]],
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    try:
        value = int(_query_value(query, name) or default)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def build_service_readiness(payload: dict) -> dict:
    gate = slip_payload_gate(
        payload,
        max_age_seconds=_env_int(
            "DASHBOARD_MAX_SLIP_AGE_SECONDS",
            DEFAULT_DASHBOARD_MAX_SLIP_AGE_SECONDS,
        ),
    )
    database = database_startup_status()
    safety = production_safety_status()
    authentication_required = dashboard_auth_enabled()
    authentication_configured = dashboard_auth_configured()
    ready = (
        gate["status"] == "ready"
        and bool(database.get("ready"))
        and safety["ready"]
        and authentication_configured
    )
    return {
        "status": "ready" if ready else "blocked",
        "service": "kalshi-research-dashboard",
        "data_gate": gate["code"],
        "generated_at": payload.get("generated_at"),
        "database": {
            "backend": database.get("backend") or database.get("dialect"),
            "state": database.get("state"),
            "ready": bool(database.get("ready")),
            "pending_versions": database.get("pending_versions", []),
        },
        "authentication": {
            "required": authentication_required,
            "configured": authentication_configured,
        },
        "production_safety": safety,
    }


def _ensure_paper_run(store: PostgresStore, run_id: str) -> bool:
    from .evaluation.paper_live import start_paper_test_run

    run = start_paper_test_run(store, run_id=run_id)
    return bool(run.get("created"))


def log_refresh_predictions(payload: dict) -> dict:
    from .evaluation.paper_live import log_forward_predictions

    run_id = os.environ.get("KALSHI_RUN_ID") or DEFAULT_KALSHI_RUN_ID
    max_payload_age_seconds = _env_int(
        "KALSHI_PAPER_MAX_PAYLOAD_AGE_SECONDS",
        DEFAULT_REFRESH_LEDGER_MAX_PAYLOAD_AGE_SECONDS,
    )
    store = create_store()
    run_created = _ensure_paper_run(store, run_id)
    result = log_forward_predictions(
        store,
        payload,
        run_id=run_id,
        max_payload_age_seconds=max_payload_age_seconds,
    )
    return {
        "ok": True,
        "run_id": result.get("run_id", run_id),
        "run_created": run_created,
        "database_backend": "postgres",
        "max_payload_age_seconds": max_payload_age_seconds,
        "attempted_predictions": result.get("attempted_predictions", 0),
        "logged_predictions": result.get("logged_predictions", 0),
        "rejected_predictions": result.get("rejected_predictions", 0),
        "duplicate_rows_ignored": result.get("duplicate_rows_ignored", 0),
        "rejection_reasons": result.get("rejection_reasons", []),
        "prediction_timestamp": result.get("prediction_timestamp"),
    }


def latest_jsonl(path: Path, limit: int = 20) -> list[dict]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def build_quality_status(payload: dict, audit_path: Path, error_path: Path) -> dict:
    generated_at = payload.get("generated_at")
    age_seconds = None
    if generated_at:
        try:
            generated = datetime.fromisoformat(str(generated_at))
            age_seconds = max(0, int(datetime.now().astimezone().timestamp() - generated.timestamp()))
        except ValueError:
            age_seconds = None
    latest_errors = latest_jsonl(error_path, 5)
    audit_rows = latest_jsonl(audit_path, 10)
    slip_counts = {
        "primary": int((payload.get("custom_slip") or {}).get("leg_count") or 0),
        "leverage": int((payload.get("leverage_slip") or {}).get("leg_count") or 0),
        "all_day": int((payload.get("all_day_slip") or {}).get("leg_count") or 0),
        "research_edge": int((payload.get("research_edge_slip") or {}).get("leg_count") or 0),
    }
    warnings = []
    if payload.get("refresh_error"):
        warnings.append("latest refresh has an error")
    if age_seconds is not None and age_seconds > 1800:
        warnings.append("data is older than 30 minutes")
    if not any(slip_counts.values()):
        warnings.append("no slips are currently built")
    source_quality_gate = build_dashboard_quality_gate(
        payload,
        audit_rows=audit_rows,
        latest_errors=latest_errors,
    )
    for reason in source_quality_gate.get("reasons", []):
        if reason not in warnings:
            warnings.append(reason)
    return {
        "status": "WATCH" if warnings else "OK",
        "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "generated_at": generated_at,
        "data_age_seconds": age_seconds,
        "slip_counts": slip_counts,
        "source_quality_gate": source_quality_gate,
        "metric_contamination_checks": {
            "manual_review_only": True,
            "auto_trade_enabled": False,
            "auto_bet_enabled": False,
            "kalshi_order_upload_enabled": False,
            "unresolved_rows_excluded_from_metrics": True,
            "rejected_rows_excluded_from_metrics": True,
        },
        "audit_events": len(audit_rows),
        "latest_errors": latest_errors,
        "warnings": warnings,
        "controls": {
            "frontend": "local responsive dashboard",
            "api": "/healthz, /readyz, /data.json, /refresh-status, /quality.json, /research-record.json, /review-packet.json, /review-packet.txt, POST /refresh",
            "cache": "short-lived file cache for public API responses",
            "rate_limit": f"manual refresh cooldown {REFRESH_COOLDOWN_SECONDS}s plus no-overlap lock",
            "audit": str(audit_path),
            "error_tracking": str(error_path),
            "security": "hosted authentication required by default; no automatic trade execution",
        },
    }


def load_payload(path: Path) -> dict:
    if not path.exists():
        return {
            "date": "",
            "games": [],
            "markets": [],
            "safety_note": "Run the today command first to generate data.",
        }
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "date": "",
            "games": [],
            "markets": [],
            "safety_note": "Data file is being refreshed. Reload in a few seconds.",
            "refresh_error": f"{type(exc).__name__}: {exc}",
        }


def blocked_dashboard_payload(code: str, message: str) -> dict:
    return {
        "date": "",
        "generated_at": None,
        "games": [],
        "markets": [],
        "safety_note": message,
        "refresh_error": code,
    }


def load_current_payload(path: Path) -> dict:
    source = str(os.environ.get("DASHBOARD_PAYLOAD_SOURCE") or "postgres").strip().lower()
    if source == "file":
        if hosted_runtime():
            return blocked_dashboard_payload(
                "hosted_file_payload_forbidden",
                "Hosted dashboards require the PostgreSQL collector snapshot.",
            )
        return load_payload(path)
    if source != "postgres":
        return blocked_dashboard_payload(
            "invalid_dashboard_payload_source",
            "The configured dashboard data source is invalid.",
        )
    try:
        payload = load_latest_kalshi_snapshot()
    except Exception as exc:
        print(f"Dashboard PostgreSQL snapshot unavailable: {type(exc).__name__}")
        return blocked_dashboard_payload(
            "postgres_snapshot_unavailable",
            "The PostgreSQL-backed Kalshi snapshot is unavailable.",
        )
    if payload is None:
        return blocked_dashboard_payload(
            "postgres_snapshot_missing",
            "No collector-backed Kalshi snapshot has been stored yet.",
        )
    return payload


def write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temporary_path.replace(path)


def cleanup_runtime_storage() -> dict:
    if not _env_flag(os.environ, "KALSHI_RUNTIME_CLEANUP_ENABLED", True):
        return {"ok": True, "skipped": True, "reason": "runtime_cleanup_disabled"}
    try:
        result = prune_http_cache()
        if result.get("deleted_files"):
            print(
                "Runtime cleanup pruned "
                f"{result.get('deleted_files')} cache files "
                f"({int(result.get('deleted_bytes') or 0)} bytes)."
            )
        return result
    except Exception as exc:
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        print(f"Runtime cleanup failed: {result['error']}")
        return result


def plural(count: object, singular: str, many: str | None = None) -> str:
    """"1 market" but "2 markets", and "-" is not a count.

    Takes the already-rendered value as well as an integer, because several
    cards show "-" when there is nothing to count, and that is not the number
    one.
    """

    return singular if str(count).strip() == "1" else (many or f"{singular}s")


def leg_label(count: object) -> str:
    """"leg" or "legs", so a one-leg slip does not read "1 LEGS"."""

    return plural(count, "leg")


def slip_is_built(slip: Mapping[str, object] | None) -> bool:
    """Whether a tier produced a slip.

    Two places count this -- the hero's "review tiers ready" and the tier
    summary's ready count -- and they render on the same page a few hundred
    pixels apart. They used to test the action field separately, so they agreed
    only for as long as nobody edited one of them: changing what counts as built
    in one spot showed a reader "1/3" beside "2/3". One definition means they
    cannot disagree.
    """
    return (slip or {}).get("action") == "BUILD_SLIP"


def finite_number(value: object) -> float | None:
    """The one place this module decides whether a value is a number.

    Every display formatter below had written its own version of this and every
    one of them had the same two holes. `float()` raises OverflowError -- not
    TypeError, not ValueError -- for an int too large to convert, so
    `10 ** 400` escaped each `except` clause and 500ed the page. And NaN and
    infinity convert without complaint, so `+nan pts`, `inf%` and `$inf`
    rendered onto the card as though they were measurements:

        percent               nan%        inf%       OverflowError
        money                 nan         inf        OverflowError
        dollars               $nan        $inf       OverflowError
        probability_points    +nan pts    +inf pts   OverflowError
        format_american_odds  nan         +inf       OverflowError

    A crash is at least visible. `+nan pts` sits on the estimate-versus-price
    card looking like a number, which is the failure this platform is most
    concerned with everywhere else.

    Returning None rather than raising lets each caller keep its own word for
    absence -- "n/a" for most, the escaped original for `money`.
    """

    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    return number if isfinite(number) else None


def unreadable_number(value: object, absent: str = "n/a") -> str:
    """What to show once `finite_number` has said no.

    These formatters deliberately echo what they were handed rather than hide
    it: an odds or line value in a format this code does not recognise -- a
    bare "EVEN", say -- is more use to a reader shown than swallowed. A
    non-finite number is not that case. NaN and infinity are numeric values
    meaning "no number", and echoing them puts `$nan` and `inf` on the page
    exactly where a measurement goes.

    The line between the two is whether the value parses as a number at all,
    and nothing else. Two versions of this branched on Python type first and
    each one leaked a different way: `isinstance(value, (int, float))` echoed
    the string `"nan"`, and adding a `str` branch beside it still echoed
    `Decimal("NaN")` -- which matters because the sports board and the
    closing-line report hand every number to this module as a Decimal.

    So there is no type test here at all. Ask `float()` and let the exception
    say which case it is.
    """

    try:
        float(value)  # type: ignore[arg-type]
    except ValueError:
        # Text the parser cannot read. This is the case the echo exists for: a
        # bare "EVEN" in an odds field is more use to a reader shown than
        # swallowed.
        return html.escape(str(value))
    except TypeError:
        # Not number-shaped at all -- a list, a dict, None. Echoing gives the
        # reader "[]" where a measurement goes.
        return absent
    except OverflowError:
        return absent
    # It parses, and `finite_number` has already refused it, so it is a number
    # that is not one: NaN or an infinity, whatever type it arrived as.
    return absent


def money(value: object) -> str:
    if value is None or value == "":
        return "n/a"
    number = finite_number(value)
    if number is None:
        return unreadable_number(value)
    return f"{number:.2f}"


def percent(value: object, decimals: int = 2) -> str:
    number = finite_number(value)
    return "n/a" if number is None else f"{number * 100:.{decimals}f}%"


def probability_points(value: object, decimals: int = 1, *, signed: bool = True) -> str:
    """The gap between two probabilities, in percentage points.

    A difference of probabilities is not a percentage of anything, and writing
    it as one is ambiguous in a way that matters here. "Difference +3.6%"
    against a 56.47% break-even reads either as 3.6 points -- what the number
    is -- or as 3.6% of 56.47%, which is 2.0 points. Nearly a factor of two, on
    the single figure the estimate-versus-price card exists to communicate.

    This platform had already settled the question elsewhere and not applied it:
    the closing-line panel quotes "+1.20 pts", and the report's own name for the
    measure is `probability_points_versus_closing_line`. Four other places said
    "%" for the same quantity.
    """

    number = finite_number(value)
    if number is None:
        return "n/a"
    return f"{number * 100:{'+' if signed else ''}.{decimals}f} pts"


def dollars(value: object) -> str:
    """A signed dollar amount, with the sign outside the currency symbol.

    ``f"${money(-0.05)}"`` renders "$-0.05". Expected value is the one figure on
    the dashboard that goes negative -- which is the ordinary result of buying
    at the ask, and so worth rendering as money rather than as a typo.
    """

    number = finite_number(value)
    if number is None:
        return money(value)
    return f"-${money(abs(number))}" if number < 0 else f"${money(number)}"


def significant_decimals(interval: object) -> int:
    """How many decimals a simulated percentage has earned, from its interval.

    A digit is worth printing when it is larger than the uncertainty on the
    figure carrying it. The slip simulation already reports a 95% interval, so
    the half-width answers this directly: at +/-0.39 points the hundredths digit
    is noise, and printing "27.22%" claims resolution the estimator missed by a
    factor of forty.

    Two decimals stays the ceiling rather than the default -- it is what an
    exact figure gets, and what a simulation gets only once its band is under a
    hundredth of a point.
    """

    try:
        low, high = float(interval[0]), float(interval[1])  # type: ignore[index]
    except (TypeError, ValueError, IndexError, KeyError):
        return 2
    half_width_points = abs(high - low) * 100.0 / 2.0
    if half_width_points >= 1.0:
        return 0
    if half_width_points >= 0.1:
        return 1
    return 2


def combo_probability_display(slip: object) -> tuple[str, str]:
    """A slip's joint probability, and the band it is quoted within.

    ``today.py`` computes this two different ways and says which on the slip.
    When the legs share nothing the correlation model recognises, the joint is
    the exact product of the leg prices and two decimals are earned. When they
    do, it is the product plus a simulated correlation adjustment -- and that
    adjustment carries a standard error the payload has always reported and this
    page has never read. The comment on it in ``slip_analysis`` is explicit that
    it exists so "a consumer rendering the headline probability must be able to
    see that it is not yet worth quoting"; this is that consumer.

    The exact product is the control variate, so the estimate's error is the
    adjustment's error and nothing else -- the interval is the point estimate
    plus or minus it.

    Returns the figure and a band, the band empty when there is no simulation
    behind the figure.
    """

    if not isinstance(slip, Mapping):
        return "n/a", ""
    try:
        value = float(slip.get("adjusted_probability") or 0.0)
    except (TypeError, ValueError):
        return "n/a", ""
    try:
        standard_error = float(slip.get("correlation_adjustment_standard_error") or 0.0)
    except (TypeError, ValueError):
        standard_error = 0.0

    basis = str(slip.get("joint_basis") or "")
    if basis.startswith("exact_product") or not isfinite(standard_error) or standard_error <= 0.0:
        return f"{value * 100:.2f}%", ""

    half_width = 1.959964 * standard_error
    interval = [max(0.0, value - half_width), min(1.0, value + half_width)]
    decimals = significant_decimals(interval)
    band = (
        f"95% CI {interval[0] * 100:.{decimals}f}-{interval[1] * 100:.{decimals}f}%"
    )
    # An unresolved adjustment means the draws could not establish even the sign
    # of the correlation term. The point estimate is still the best one
    # available, which is why it is shown -- but a reader has to know the
    # difference between "the correction is small" and "we could not measure
    # the correction".
    if not slip.get("correlation_adjustment_resolved", True):
        band = f"{band} · correlation unresolved"
    return f"{value * 100:.{decimals}f}%", f'<small class="metric-range">{html.escape(band)}</small>'


def display_timestamp(value: object) -> str:
    if not value:
        return "pending"
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return html.escape(str(value))
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone()
    return stamp.strftime("%b %d, %I:%M %p").replace(" 0", " ").replace(", 0", ", ")


def timestamp_element(value: object) -> str:
    """Render a build timestamp the browser re-formats in the reader's zone.

    The server renders in its own zone -- UTC when hosted -- while event times
    were already localized on the client, so one page showed two clocks. The
    server text stays as the no-script fallback.
    """
    text = display_timestamp(value)
    if not value:
        return html.escape(text)
    return (
        f'<time datetime="{html.escape(str(value), quote=True)}" data-format="timestamp">'
        f"{html.escape(text)}</time>"
    )


def display_event_time(value: object) -> str:
    if not value:
        return "Time TBD"
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return "Time TBD"
    if stamp.tzinfo is not None:
        today = datetime.now(stamp.tzinfo).date()
    else:
        today = datetime.now().date()
    day_delta = (stamp.date() - today).days
    if day_delta == 0:
        day_text = "Today"
    elif day_delta == 1:
        day_text = "Tomorrow"
    else:
        day_text = stamp.strftime("%b %d").replace(" 0", " ")
    time_text = stamp.strftime("%I:%M %p").lstrip("0")
    return f"{day_text} · {time_text}"


def slip_copy_text(slip: dict, label: str) -> str:
    if slip.get("action") != "BUILD_SLIP":
        return f"{label}\nNo slip generated."
    lines = [
        label,
        f"Legs: {slip.get('leg_count', 0)}",
        f"Est. price: {money(slip.get('estimated_combo_price_cents'))}c",
        f"$5 payout if right: ${money(slip.get('estimated_payout_if_right'))}",
        "",
    ]
    for index, leg in enumerate(slip.get("legs", []), start=1):
        event = leg.get("display_event") or leg.get("event_ticker") or "Unknown event"
        side = str(leg.get("side", "")).upper()
        label_text = leg.get("subtitle") or leg.get("title") or leg.get("market_ticker", "")
        probability = percent(leg.get("probability"))
        if leg.get("research_probability") is not None:
            kalshi = percent(leg.get("kalshi_probability"))
            margin = percent(leg.get("margin_of_error"))
            evidence_count = leg.get("evidence_count", 0)
            lines.append(f"{index}. {event} - {side} {label_text} (model {probability}, Kalshi {kalshi}, +/-{margin}, {evidence_count} sources)")
        else:
            lines.append(f"{index}. {event} - {side} {label_text} ({probability})")
    return "\n".join(lines)


ICON_PATHS = {
    "builder": "M4 19V9m5 10V5m5 14v-7m5 7V8",
    "contracts": "M12 3 20 8v8l-8 5-8-5V8z M12 3v18 M4 8l8 5 8-5",
    "sports": "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Z M12 3v18 M3.5 9h17 M3.5 15h17",
    "slip": "M5 4h14v16l-3-2-2 2-2-2-2 2-3-2z M9 9h6 M9 13h6",
    "clock": "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Z M12 7v5l3 2",
    "scout": "M12 3v3 M12 18v3 M3 12h3 M18 12h3 M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8Z",
    "health": "M3 12h4l2-5 3 10 2-5h7",
    "record": "M4 20V4 M4 20h16 M8 16l4-5 3 3 5-7",
}


def icon(name: str) -> str:
    """Outline icon from the design system's 1.75px-stroke set."""
    path = ICON_PATHS.get(name, ICON_PATHS["builder"])
    return (
        '<svg viewBox="0 0 24 24" aria-hidden="true" stroke-linecap="round" '
        f'stroke-linejoin="round"><path d="{path}"/></svg>'
    )


FAVICON_LINK = (
    '<link rel="icon" href="data:image/svg+xml,'
    "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 48 48'%3E"
    "%3Crect width='48' height='48' rx='10' fill='%23080f14'/%3E"
    "%3Cpath fill='%2300e676' d='M8 23c6-13 21-16 32-11-6.5.4-11 2.4-13.8 5.9 4.8-1.8 9.1-1.5 12.9.9-6.7 1-11.6 3.6-14.8 7.7 4.1-1.5 7.8-1.1 11.1 1-6.4.7-10.9 3-13.5 7-2.4-3.2-3.4-6.6-2.9-10.4-2.8 2.7-4.3 5.9-4.6 9.8C9.9 30.7 8.1 27.1 8 23Z'/%3E"
    '%3C/svg%3E">'
)


def render_brand(href: str = "#builder") -> str:
    return f"""
    <a class="brand" href="{html.escape(href, quote=True)}" aria-label="Hawknetic Predictions">
      <svg class="brand-mark" viewBox="0 0 48 48" aria-hidden="true">
        <path d="M6 22.5C12.5 8 29.5 4 42 10c-7.5.5-12.8 2.8-16 6.8 5.6-2.1 10.6-1.7 15 1-7.8 1.2-13.5 4.2-17.2 9 4.8-1.7 9.1-1.3 12.9 1.2-7.4.8-12.6 3.5-15.6 8.1-2.8-3.7-4-7.7-3.4-12.1-3.2 3.1-5 6.9-5.4 11.4C8.2 31.9 6.1 27.7 6 22.5Z"/>
      </svg>
      <span>Hawknetic<strong>Predictions</strong></span>
    </a>
    """


def render_market_browser(payload: dict) -> str:
    markets = list(payload.get("markets") or [])
    gate = payload.get("public_data_gate") or {}
    if gate.get("status") != "ready":
        return f"""
        <div class="empty-state">
          <span class="empty-state-icon" aria-hidden="true">!</span>
          <strong>Kalshi contracts temporarily hidden</strong>
          <p>{html.escape(str(gate.get("message") or "Fresh Kalshi evidence is required before contracts can be shown."))}</p>
        </div>
        """
    if not markets:
        heading = "No verified contracts available"
        message = "The current Kalshi response is fresh, but no exact listed combo contracts meet the review requirements."
        return f"""
        <div class="empty-state">
          <span class="empty-state-icon" aria-hidden="true">⌁</span>
          <strong>{html.escape(heading)}</strong>
          <p>{html.escape(message)}</p>
        </div>
        """
    visible_markets = sorted(
        markets,
        key=lambda market: (
            not bool(market.get("real_data_ready")),
            -(float(market.get("volume_24h") or 0) if str(market.get("volume_24h") or "").replace(".", "", 1).isdigit() else 0),
            str(market.get("ticker") or ""),
        ),
    )[:8]
    rows = "".join(render_market_browser_row(market) for market in visible_markets)
    return f'<div class="data-rows">{rows}</div>'


def render_market_browser_row(market: dict) -> str:
    ticker = str(market.get("ticker") or "Unidentified contract")
    title = str(market.get("title") or ticker)
    legs = list(market.get("leg_details") or [])
    leg_count = len(legs) or len(market.get("legs") or [])
    ready = bool(market.get("real_data_ready"))
    status_text = "Verified" if ready else "Incomplete"
    status_class = "good" if ready else "warning"
    close_text = display_event_time(market.get("close_time"))
    detail_items = "".join(render_market_preview_leg(leg) for leg in legs)
    if not detail_items:
        detail_items = '<li>Underlying leg details are not available.</li>'
    return f"""
    <article class="data-row">
      <div class="row-heading">
        <span class="contract-orb" aria-hidden="true"></span>
        <div>
          <strong>{html.escape(title)}</strong>
          <small>{html.escape(ticker)} · {leg_count} exact legs · closes {html.escape(close_text)}</small>
        </div>
      </div>
      <div class="quote-cell"><small>YES ask</small><strong>{money(market.get("yes_ask_cents"))}c</strong></div>
      <div class="quote-cell"><small>NO ask</small><strong>{money(market.get("no_ask_cents"))}c</strong></div>
      <div class="quote-cell"><small>24h volume</small><strong>{html.escape(str(market.get("volume_24h") or "n/a"))}</strong></div>
      <span class="badge {status_class}">{status_text}</span>
      <details class="row-details">
        <summary>Inspect listed legs</summary>
        <ul>{detail_items}</ul>
        <p>{html.escape(str(market.get("real_data_warning") or "Prices come straight from Kalshi."))}</p>
      </details>
    </article>
    """


def render_market_preview_leg(leg: dict) -> str:
    event = leg.get("display_event") or leg.get("event_ticker") or "Event"
    label = leg.get("subtitle") or leg.get("title") or leg.get("market_ticker") or "Market"
    probability = leg.get("market_implied_probability")
    probability_text = "n/a" if probability is None else f"{float(probability) * 100:.1f}%"
    return (
        "<li>"
        f"<span><strong>{html.escape(str(event))}</strong><small>{html.escape(str(leg.get('side') or '').upper())} · {html.escape(str(label))}</small></span>"
        f"<b>{html.escape(probability_text)}</b>"
        "</li>"
    )


SPORTS_BOARD_STATE_COPY = {
    "blocked": (
        "Sports collection is blocked",
        "The public sports source refused or failed its last collection. No rows are shown until it recovers.",
    ),
    "stale": (
        "Sports odds are stale",
        "The newest uploaded sports row is older than the freshness window, so the board is withheld rather than shown as current.",
    ),
    "unavailable": (
        "No sports rows uploaded yet",
        "The sports-research worker has not written any validated rows to this database.",
    ),
    "empty": (
        "No upcoming games with posted odds",
        "The collector is fresh, but every uploaded game has already started or has no usable odds.",
    ),
}


# Compression is opt-in per response rather than a rule keyed on content type.
# `send_json` answers the login POST with a CSRF token in its body, and
# compressing a secret in the same response as text an attacker can influence is
# the precondition BREACH needs; nothing is gained there anyway, since those
# responses are small and sent once. Only the dashboard HTML and the static
# assets take this path, and neither carries a secret.
#
# Below roughly a kilobyte the gzip header and trailer cost more than the
# encoding saves, and level 6 is the usual knee: on a 40KB page level 9 spends
# noticeably more CPU per request for well under a percent more compression.
MIN_COMPRESSIBLE_BYTES = 1024
COMPRESSION_LEVEL = 6


STATE_REASON_COPY = {
    "sports_board_unavailable": "The sports database could not be read while this page was built.",
    "sports_clv_unavailable": "Closing-line results could not be read while this page was built.",
}


UNEXPLAINED_STATE_REASON = "This section could not be read while the page was built."


def explain_state_reason(reason: str, *, technical: bool = True) -> str:
    """Turn an internal reason code into something the viewer can act on.

    These arrive as diagnostics like `sports_board_unavailable:OperationalError`.
    The exception class is worth showing to someone who can go and look at the
    service; to a reader it is a Python type name attached to no action they can
    take, sitting inside a warning box that already told them what happened. So
    `technical` follows the viewer's role: operators keep the class name in the
    sentence, readers get the sentence. Either way both call sites also carry the
    raw code in a `title`, so nothing is lost for whoever inspects the page.
    """
    text = str(reason or "").strip()
    if not text:
        return ""
    code, _, detail = text.partition(":")
    explained = STATE_REASON_COPY.get(code)
    if explained is None:
        # An unmapped code is meaningless to a reader, so it degrades to the
        # generic sentence rather than surfacing an internal identifier.
        return text if technical else UNEXPLAINED_STATE_REASON
    if technical and detail.strip():
        return f"{explained} ({detail.strip()})"
    return explained


def safe_sports_board() -> dict:
    """Load the sports board, degrading to an explicit unavailable state.

    An optional read must never make a healthy Kalshi dashboard look broken, so a
    database or collector problem becomes a named state instead of a 500.
    """
    try:
        return load_sports_board()
    except Exception as exc:  # noqa: BLE001 - surfaced to the operator as a board state
        return {
            "asset_class": "sports",
            "board_state": "unavailable",
            "state_reason": f"sports_board_unavailable:{type(exc).__name__}",
            "is_current": False,
            "events": [],
            "event_count": 0,
            "withheld_event_count": 0,
            "quote_count": 0,
            "source_health": [],
            "worker": None,
            "model_state": "baseline_only",
            "decision_status": "track_only",
        }


def safe_sports_clv_report() -> dict:
    """Load the CLV report, degrading to an empty one on any database problem."""
    try:
        return build_sports_clv_report()
    except Exception as exc:  # noqa: BLE001 - surfaced as an empty report, not a 500
        return {
            "graded_rows": 0,
            "pending_rows": 0,
            "beat_close": 0,
            "lost_to_close": 0,
            "matched_close": 0,
            "beat_rate": None,
            "beat_rate_denominator": 0,
            "average_clv": None,
            "average_clv_interval": None,
            "average_clv_sample": 0,
            "by_market": [],
            "by_bookmaker": [],
            "unavailable_reason": f"sports_clv_unavailable:{type(exc).__name__}",
        }


def render_sports_clv_panel(report: dict, *, technical: bool = True) -> str:
    graded = int(report.get("graded_rows") or 0)
    if graded == 0:
        raw_reason = str(report.get("unavailable_reason") or "")
        reason = explain_state_reason(raw_reason, technical=technical) or (
            "No sports market has closed yet, so no price can be compared against a closing line."
        )
        title_attribute = f' title="{html.escape(raw_reason, quote=True)}"' if raw_reason else ""
        return f"""
        <div class="decision warning">
          <div class="status-heading"><strong>No closing lines recorded</strong><span>0 graded</span></div>
          <p class="status-note"{title_attribute}>{html.escape(reason)}</p>
        </div>
        """
    beat = int(report.get("beat_close") or 0)
    lost = int(report.get("lost_to_close") or 0)
    matched = int(report.get("matched_close") or 0)
    beat_rate = report.get("beat_rate")
    beat_rate_text = "n/a" if beat_rate in {None, ""} else percent(beat_rate, 1)
    # The denominator is decided rows -- beat plus lost -- not the graded count
    # shown beside it, because a row that matched the close decided nothing. It
    # was computed and dropped, so the rate sat on the card with no sample
    # attached and no way for a reader to arrive at it from the other cells.
    decided = int(report.get("beat_rate_denominator") or 0)
    beat_interval = wilson_interval(beat, decided) if decided else None
    beat_rate_range = (
        f'<small class="metric-range">95% CI {float(beat_interval[0]) * 100:.0f}-'
        f'{float(beat_interval[1]) * 100:.0f}% on {decided} decided</small>'
        if beat_interval
        else ""
    )
    # Through the same helper the per-market and per-bookmaker rows below use.
    #
    # The headline used to compute its own text, falling back to 0.0 for a
    # missing average and rendering it as "+0.00 pts". `AVG(clv)` is SQL NULL
    # whenever no row is graded, so that is the report's ordinary empty state,
    # and the largest number on the panel was announcing a measured zero for it
    # -- while the small rows beside it correctly read "n/a" from this helper.
    #
    # Worse in company: the interval is computed separately, so a report with no
    # average and an interval showed "+0.00 pts" beside "95% CI +0.80 to +1.60",
    # a point estimate outside its own confidence interval. No sample produces
    # that.
    average = report.get("average_clv")
    average_available = average not in {None, ""}
    average_text = _clv_points_text(average) if average_available else "n/a"
    average_heading = f"{average_text} average" if average_available else "No average yet"

    # Beating the close more often than not is the signal worth showing; it is
    # still a price comparison, never a profitability claim.
    #
    # The panel used to go green on the sign of the average alone. CLV per row is
    # noisy in both directions, so a handful of rows average positive about half
    # the time on no edge at all -- and green is this dashboard's word for a
    # result. It now takes an interval that clears zero, which is the weakest
    # claim that is still a claim. Without an interval at all (one row, or a
    # degraded report) it stays neutral rather than inheriting the benefit of the
    # doubt.
    average_interval = report.get("average_clv_interval")
    sample_size = int(report.get("average_clv_sample") or 0)
    # Green is this dashboard's word for a result, so it needs both halves of
    # the statistic. An interval whose point estimate is missing is not a weaker
    # claim than usual, it is a claim about a number the report does not have.
    average_beats_zero = (
        average_available and bool(average_interval) and float(average_interval[0]) > 0
    )
    decision_class = "good" if average_beats_zero else "warning"
    # An interval is a claim about where a point estimate sits, so it is not
    # presentable without one. Both come from the same report, but they are
    # separate keys and a degraded report can carry either alone.
    average_range = (
        f'<small class="metric-range">95% CI {float(average_interval[0]) * 100:+.2f} to '
        f'{float(average_interval[1]) * 100:+.2f} pts on {sample_size} {plural(sample_size, "row")}</small>'
        if average_interval and average_available
        else ""
    )
    market_rows = "".join(
        f"<li><span><strong>{html.escape(str(entry.get('market_type') or 'market'))}</strong>"
        f"<small>{int(entry.get('graded_rows') or 0)} graded · {int(entry.get('beat_close') or 0)} beat close</small></span>"
        f"<b>{_clv_points_text(entry.get('average_clv'))}</b></li>"
        for entry in (report.get("by_market") or [])
    )
    book_rows = "".join(
        f"<li><span><strong>{html.escape(str(entry.get('bookmaker') or 'book'))}</strong>"
        f"<small>{int(entry.get('graded_rows') or 0)} graded · {int(entry.get('beat_close') or 0)} beat close</small></span>"
        f"<b>{_clv_points_text(entry.get('average_clv'))}</b></li>"
        for entry in (report.get("by_bookmaker") or [])[:6]
    )
    return f"""
    <div class="decision {decision_class}">
      <div class="status-heading"><strong>Closing line value</strong><span>{average_heading}</span></div>
      <p class="status-note">Price comparison against each market's last pre-start quote. Not profit and not a settled result.</p>
      <div class="metric-strip">
        <span><small>Graded</small><strong>{graded}</strong></span>
        <span><small>Average CLV</small><strong>{average_text}</strong>{average_range}</span>
        <span><small>Beat close</small><strong>{beat}</strong></span>
        <span><small>Lost to close</small><strong>{lost}</strong></span>
        <span><small>Matched</small><strong>{matched}</strong></span>
        <span><small>Beat rate</small><strong>{html.escape(beat_rate_text)}</strong>{beat_rate_range}</span>
        <span><small>Awaiting close</small><strong>{int(report.get("pending_rows") or 0)}</strong></span>
      </div>
      <details class="row-details">
        <summary>Break down by market and book</summary>
        <ul>{market_rows}{book_rows}</ul>
      </details>
    </div>
    """


def _clv_points_text(value: object) -> str:
    return probability_points(value, 2)


def format_american_odds(value: object) -> str:
    """Render an exact stored price as a bettor reads it (+110, -120)."""
    if value is None or (isinstance(value, str) and not value):
        return "n/a"
    number = finite_number(value)
    if number is None:
        return unreadable_number(value)
    rendered = f"{number:g}"
    return f"+{rendered}" if number > 0 else rendered


def render_sports_section(board: dict, *, technical: bool = True) -> str:
    if not board.get("is_current"):
        state = str(board.get("board_state") or "unavailable")
        heading, message = SPORTS_BOARD_STATE_COPY.get(
            state, ("Sports board unavailable", "The sports board could not be built from the current database state.")
        )
        reason = str(board.get("state_reason") or "")
        withheld = int(board.get("withheld_event_count") or 0)
        withheld_note = (
            f"<p>{withheld} collected event(s) are held back because they are not current.</p>" if withheld else ""
        )
        reason_html = (
            f'<p class="state-reason" title="{html.escape(reason, quote=True)}">'
            f"{html.escape(explain_state_reason(reason, technical=technical))}</p>"
            if reason
            else ""
        )
        return f"""
        <div class="empty-state">
          <span class="empty-state-icon" aria-hidden="true">◐</span>
          <strong>{html.escape(heading)}</strong>
          <p>{html.escape(message)}</p>
          {withheld_note}
          {reason_html}
        </div>
        """
    events = list(board.get("events") or [])[:8]
    rows = "".join(render_sports_event(event) for event in events)
    return f'<div class="sports-board-list">{rows}</div>'


def render_sports_event(event: dict) -> str:
    markets = "".join(render_sports_market(market) for market in event.get("markets") or [])
    league = str(event.get("league") or "").upper()
    start_text = display_event_time(event.get("game_start_time"))
    market_count = int(event.get("market_count") or 0)
    return f"""
    <article class="sports-event">
      <div class="sports-event-heading">
        <span class="contract-orb" aria-hidden="true"></span>
        <div>
          <strong>{html.escape(str(event.get("away_team") or "Away"))} @ {html.escape(str(event.get("home_team") or "Home"))}</strong>
          <small>{html.escape(league)} · {html.escape(start_text)} · {market_count} {plural(market_count, "market")}</small>
        </div>
      </div>
      <div class="sports-market-list">{markets}</div>
    </article>
    """


def format_market_line(value: object, market_type: str) -> str:
    """Spreads carry a sign; totals are a bare number ("8.5", not "+8.5")."""
    if value is None or (isinstance(value, str) and not value):
        return ""
    number = finite_number(value)
    if number is None:
        return unreadable_number(value, "")
    rendered = f"{number:g}"
    if "spread" in market_type.strip().lower() and number > 0:
        return f"+{rendered}"
    return rendered


def render_sports_market(market: dict) -> str:
    market_type = str(market.get("market_type") or "market")
    selections = "".join(
        render_sports_selection(entry, market_type) for entry in market.get("selections") or []
    )
    # Spread sides carry their own signed line, so the heading stays unqualified.
    line_rendered = "" if "spread" in market_type.lower() else format_market_line(market.get("line"), market_type)
    line_text = f" {line_rendered}" if line_rendered else ""
    overround = market.get("overround")
    if market.get("no_vig_available"):
        vig_text = f"Overround {percent(overround, 2)}"
    else:
        vig_text = "One-sided market · no de-vig"
    books = int(market.get("bookmaker_count") or 0)
    consensus_books = int(market.get("consensus_bookmaker_count") or 0)
    if consensus_books:
        vig_text = f"{vig_text} · consensus of {consensus_books}"
    return f"""
    <div class="sports-market">
      <div class="sports-market-head">
        <strong>{html.escape(market_type.title())}{html.escape(line_text)}</strong>
        <small>{books} book{"s" if books != 1 else ""} · {html.escape(vig_text)}</small>
      </div>
      <div class="sports-selection-list">{selections}</div>
    </div>
    """


def render_sports_selection(entry: dict, market_type: str = "") -> str:
    fair = entry.get("no_vig_probability")
    fair_text = "n/a" if fair in {None, ""} else percent(fair, 1)
    consensus = entry.get("consensus_probability")
    consensus_text = "n/a" if consensus in {None, ""} else percent(consensus, 1)
    gain = entry.get("line_shopping_gain_probability")
    try:
        gain_value = float(gain) if gain not in {None, ""} else 0.0
    except (TypeError, ValueError):
        gain_value = 0.0
    gain_html = (
        f'<span class="badge good sports-shop-pill">Shop {probability_points(gain_value)}</span>' if gain_value > 0 else ""
    )
    # Only a positive gap is shown as one. A negative gap is the ordinary state
    # of a priced market and would read as a signal if it were given a pill.
    try:
        raw_gap = entry.get("best_price_vs_consensus_probability")
        gap_value = float(raw_gap) if raw_gap not in {None, ""} else 0.0
    except (TypeError, ValueError):
        gap_value = 0.0
    gap_html = (
        f'<span class="badge good sports-shop-pill" title="Best posted price implies less probability '
        f'than the books\' own consensus. A price comparison, not a validated edge.">'
        f'vs consensus {probability_points(gap_value)}</span>'
        if gap_value > 0
        else ""
    )
    line_rendered = format_market_line(entry.get("line"), market_type)
    name = str(entry.get("selection") or "Selection")
    label = f"{name} {line_rendered}" if line_rendered else name
    return f"""
    <div class="sports-selection">
      <span class="sports-selection-name">{html.escape(label)}</span>
      <span class="sports-selection-odds">{html.escape(format_american_odds(entry.get("best_odds")))}</span>
      <span class="sports-selection-book">{html.escape(str(entry.get("best_bookmaker") or "n/a"))}</span>
      <span class="sports-selection-fair"><small>No-vig</small><b>{html.escape(fair_text)}</b></span>
      <span class="sports-selection-fair"><small>Consensus</small><b>{html.escape(consensus_text)}</b></span>
      {gain_html}
      {gap_html}
    </div>
    """


def slip_analysis_report(slip: dict, payload: dict, slip_key: str) -> dict:
    """Use one bounded, failure-aware analysis path for every slip view."""
    if slip_key not in SLIP_SOURCES:
        return {"analysis_available": False, "detail": "Unknown review tier."}
    try:
        return build_slip_analysis(
            {**payload, SLIP_SOURCES[slip_key][0]: slip},
            slip_key,
            stake=DEFAULT_SLIP_STAKE_DOLLARS,
        )
    except Exception as error:
        return {"analysis_available": False, "detail": f"Slip arithmetic unavailable: {type(error).__name__}."}


def slip_estimate_display(report: dict) -> tuple[str, str]:
    """Format the same estimate and interval in the card, tile and drawer.

    A partial analysis describes fewer legs than the listed slip. Compact
    views cannot silently present it as a probability for the complete slip.
    """
    if not report.get("analysis_available") or report.get("skipped_legs"):
        return "Unavailable", ""
    analysis = report.get("analysis") or {}
    value = finite_number(analysis.get("hit_probability"))
    if value is None or not 0 <= value <= 1:
        return "Unavailable", ""
    interval = analysis.get("hit_probability_interval")
    decimals = significant_decimals(interval)
    band = (
        f'<small class="metric-range">95% CI '
        f'{float(interval[0]) * 100:.{decimals}f}-{float(interval[1]) * 100:.{decimals}f}%</small>'
        if interval else ""
    )
    return f"{value * 100:.{decimals}f}%", band


def render_compact_slip(
    slip: dict, source_payload: dict, *, show_dollar_figures: bool = True,
    analysis_report: dict | None = None,
) -> str:
    if slip.get("action") != "BUILD_SLIP":
        reason = str(slip.get("reason") or "No exact listed combo currently meets the review rules.")
        source_context = combo_source_context(source_payload, "primary")
        return f"""
        <div class="drawer-empty-state">
          <span class="drawer-warning" aria-hidden="true">!</span>
          <strong>No verified primary slip</strong>
          <p>{html.escape(reason)}</p>
          {f'<small>{html.escape(source_context)}</small>' if source_context else ''}
        </div>
        <a class="drawer-secondary-action" href="#market-browser">Review live contracts</a>
        """
    label = "PRIMARY 80c+ REVIEW SLIP"
    fallback_copy_text = slip_copy_text(slip, label)
    review_packet = build_review_packet(
        {
            "date": source_payload.get("date"),
            "generated_at": source_payload.get("generated_at"),
            "generated_at_note": source_payload.get("generated_at_note"),
            "custom_slip": slip,
        },
        "primary",
    )
    review_text = review_packet.get("copy_blocks", {}).get("review_packet") or fallback_copy_text
    compact_legs = "".join(render_compact_slip_leg(leg) for leg in list(slip.get("legs") or [])[:10])
    hidden_leg_count = max(0, int(slip.get("leg_count") or 0) - 10)
    compatibility = slip.get("combo_compatibility") or {}
    manual_ready = bool(compatibility.get("manual_entry_ready", slip.get("manual_entry_ready")))
    status_text = "Ready to review" if manual_ready else "Review required"
    status_class = "good" if manual_ready else "warning"
    combo_chance_text, combo_chance_range = combo_probability_display(slip)
    report = analysis_report if analysis_report is not None else slip_analysis_report(slip, source_payload, "primary")
    estimate_text, estimate_range = slip_estimate_display(report)
    # What $5 turns into is a wagering figure, and "payout" is a wagering word.
    # A reader's drawer says what the contract costs and what that price
    # implies; the return on a hypothetical stake stays on the operator's page.
    payout_cell = (
        f'<span><small>Est. $5 payout</small><strong>${money(slip.get("estimated_payout_if_right"))}</strong></span>'
        if show_dollar_figures
        else ""
    )
    return f"""
    <div class="drawer-slip-state">
      <span class="badge {status_class}">{status_text}</span>
      <strong>{int(slip.get("leg_count") or 0)} listed legs</strong>
      <small>One exact active Kalshi combo contract</small>
    </div>
    <div class="drawer-alert">
      <span aria-hidden="true">△</span>
      <p>Research only. Check every side, price, and start time against Kalshi before relying on it.</p>
    </div>
    <div class="drawer-metrics{'' if show_dollar_figures else ' is-two-up'}">
      <span><small>Estimated to hit</small><strong>{estimate_text}</strong>{estimate_range}</span>
      <span><small>Price</small><strong>{money(slip.get("estimated_combo_price_cents"))}c</strong></span>
      {payout_cell}
    </div>
    <p class="status-note">Market implied: {combo_chance_text}. Estimate uses leg prices and correlation assumptions; it is not a validated forecast.</p>
    {combo_chance_range}
    <ul class="drawer-leg-list">{compact_legs}</ul>
    {f'<p class="drawer-more">+{hidden_leg_count} more listed legs in the full review</p>' if hidden_leg_count else ''}
    <button type="button" class="btn btn-primary copy" data-copy="{html.escape(review_text, quote=True)}">Copy Review Packet</button>
    <div class="drawer-action-row">
      <a href="#primary">Full slip details</a>
      <a href="/review-packet.txt?slip=primary" download>Download TXT</a>
    </div>
    """


def render_compact_slip_leg(leg: dict) -> str:
    event = leg.get("display_event") or leg.get("event_ticker") or "Event"
    label = leg.get("subtitle") or leg.get("title") or leg.get("market_ticker") or "Market"
    start_time = leg.get("event_start_time") or ""
    probability = float(leg.get("probability") or 0) * 100
    return f"""
    <li>
      <span class="leg-status-dot" aria-hidden="true"></span>
      <div><strong>{html.escape(str(event))}</strong><small>{html.escape(str(leg.get("side") or "").upper())} · {html.escape(str(label))}</small><time datetime="{html.escape(str(start_time), quote=True)}">{html.escape(display_event_time(start_time))}</time></div>
      <b>{probability:.1f}%</b>
    </li>
    """


def build_source_data_preview(store: SourceDataStore | None = None) -> dict:
    source_store = store or SourceDataStore()
    return {
        "summary": source_store.summary(),
        "players": source_store.list_entities(entity_type="player", limit=4),
        "teams": source_store.list_entities(entity_type="team", limit=4),
        "markets": source_store.list_external_markets(limit=4),
        "live": source_store.list_live_data(limit=4),
    }


def render_source_data_panel(preview: Mapping[str, object], *, can_refresh: bool) -> str:
    summary = preview.get("summary") if isinstance(preview.get("summary"), Mapping) else {}
    counts = summary.get("counts") if isinstance(summary.get("counts"), Mapping) else {}

    def records(name: str) -> list[Mapping[str, object]]:
        collection = preview.get(name)
        if not isinstance(collection, Mapping):
            return []
        return [row for row in collection.get("items") or [] if isinstance(row, Mapping)]

    def entity_rows(name: str) -> str:
        rows = records(name)
        if not rows:
            return '<li class="source-data-empty">No fresh rows yet.</li>'
        return "".join(
            "<li><div><strong>"
            + html.escape(str(row.get("display_name") or "Unnamed"))
            + "</strong><small>"
            + html.escape(str(row.get("competition") or row.get("entity_type") or "Source entity"))
            + "</small></div><span>"
            + html.escape(str(row.get("source") or ""))
            + "</span></li>"
            for row in rows
        )

    market_rows = records("markets")
    market_html = "".join(
        "<li><div><strong>"
        + html.escape(str(row.get("question") or "Market"))
        + "</strong><small>"
        + html.escape(str(row.get("market_type") or "sports market"))
        + " · "
        + html.escape(display_event_time(row.get("game_start_time")))
        + "</small></div><span>Polymarket</span></li>"
        for row in market_rows
    ) or '<li class="source-data-empty">No fresh Polymarket rows yet.</li>'
    live_rows = records("live")
    live_player_groups = sum(bool(row.get("player_stats")) for row in live_rows)
    refresh_button = (
        '<button class="btn btn-sm" id="refresh-source-data" type="button">Update cloud data</button>'
        if can_refresh
        else ""
    )
    return f"""
      <div class="source-data-actions">
        <p id="source-data-status" role="status" aria-live="polite">PostgreSQL source catalog</p>
        {refresh_button}
      </div>
      <div class="stat-grid" role="group" aria-label="Connected source data summary">
        <div class="stat-card"><small>Players</small><strong>{int(counts.get('players') or 0)}</strong></div>
        <div class="stat-card"><small>Teams</small><strong>{int(counts.get('teams') or 0)}</strong></div>
        <div class="stat-card"><small>Polymarket</small><strong>{int(counts.get('external_markets') or 0)}</strong><span class="stat-foot">source-backed markets</span></div>
        <div class="stat-card"><small>Live player groups</small><strong>{live_player_groups}</strong><span class="stat-foot">fresh Kalshi live snapshots</span></div>
      </div>
      <div class="source-data-grid">
        <article><div class="card-head"><h3>Players</h3><span class="badge badge-neutral">Kalshi</span></div><ul>{entity_rows('players')}</ul></article>
        <article><div class="card-head"><h3>Teams</h3><span class="badge badge-neutral">Polymarket</span></div><ul>{entity_rows('teams')}</ul></article>
        <article><div class="card-head"><h3>Current external markets</h3><span class="badge badge-neutral">Free source</span></div><ul>{market_html}</ul></article>
      </div>
    """


def render_dashboard(
    payload: dict,
    refresh_seconds: int = 0,
    *,
    principal: AuthPrincipal | None = None,
    source_data_preview: Mapping[str, object] | None = None,
) -> str:
    payload = safe_dashboard_payload(payload)
    games = payload.get("games", [])
    markets = payload.get("markets", [])
    primary_slip = payload.get("custom_slip") or {}
    leverage_slip = payload.get("leverage_slip") or {}
    all_day_slip = payload.get("all_day_slip") or {}
    research_edge_slip = payload.get("research_edge_slip") or {}
    refresh_seconds = max(0, int(refresh_seconds or 0))
    refresh_meta = f'<meta http-equiv="refresh" content="{refresh_seconds}">' if refresh_seconds else ""
    generated_at_html = timestamp_element(payload.get("generated_at"))
    refresh_label = f"Every {refresh_seconds // 60} min" if refresh_seconds else "Manual"
    refresh_error = payload.get("refresh_error")
    refresh_error_html = (
        '<p class="subtle strong-note">Live refresh delayed. Slips are hidden until fresh data returns.</p>'
        if refresh_error
        else ""
    )
    quality_status = build_quality_status(
        payload,
        repo_path("data", "refresh_audit.jsonl"),
        repo_path("data", "error_events.jsonl"),
    )
    public_data_gate = payload.get("public_data_gate") or {}
    data_is_ready = public_data_gate.get("status") == "ready" and not refresh_error
    data_state = "ready" if data_is_ready else "blocked"
    data_label = "Fresh data" if data_is_ready else "Review blocked"
    data_message = str(public_data_gate.get("message") or "Fresh data is required before slips can be reviewed.")
    data_message_html = (
        f'<p class="data-state-message">{html.escape(data_message)}</p>' if not data_is_ready else ""
    )
    research_record = build_research_record(payload=payload)
    sports_board = safe_sports_board()
    sports_clv = safe_sports_clv_report()
    sports_summary = summarize_sports_board(sports_board)
    sports_summary_text = (
        f"{sports_summary['event_count']} upcoming "
        f"{plural(sports_summary['event_count'], 'game')} · "
        f"{sports_summary['no_vig_market_count']} de-vigged "
        f"{plural(sports_summary['no_vig_market_count'], 'market')} · "
        f"{sports_summary['line_shopping_market_count']} priced by more than one book."
        if sports_summary["is_current"]
        else "Sports rows are only shown while the collector is fresh and unblocked."
    )
    # The viewer's role decides whether the page may trigger a refresh itself
    # or must ask the reader to reload, so the script does not poll endpoints
    # its caller is not allowed to reach.
    viewer_role = str(getattr(principal, "role", "") or "read_only")
    viewer_can_refresh = role_allows(viewer_role, "admin")
    # Same bar, different reason: whoever can act on a failing service is who
    # benefits from seeing which exception it raised. A reader gets the sentence
    # without the Python type name.
    viewer_sees_diagnostics = role_allows(viewer_role, "admin")
    # Whether the page shows how the sausage is made. Collection health, source
    # freshness, worker and database state, and the settled-record ledger are
    # all operator questions: they describe the pipeline that produces the
    # picks, not the picks. A reader cannot act on any of them -- a stalled
    # worker is not their problem to fix, and the gate already withholds
    # anything unsafe to show -- so for them these panels were pure noise
    # between the tiers they came for.
    viewer_sees_operations = role_allows(viewer_role, "admin")
    # Money on the page -- what $5 would return, the expected value of that $5
    # -- is the one place the card borrows a bet slip's conventions. The
    # research finding is the estimate against the break-even, in probability
    # points, and a reader gets exactly that. The dollar restatement stays on
    # the operator's page, where it is a working figure rather than a pitch.
    viewer_sees_dollar_figures = viewer_sees_operations
    analysis_reports = {
        key: slip_analysis_report(payload.get(field) or {}, payload, key)
        for key, (field, _) in SLIP_SOURCES.items()
        if key != "research_edge" or viewer_sees_operations
    }
    source_data_panel = render_source_data_panel(
        source_data_preview or {},
        can_refresh=viewer_can_refresh,
    )
    refresh_control_html = (
        """<div class="refresh-control">
        <button id="refresh-slip" class="btn btn-primary btn-sm" type="button"><span aria-hidden="true">↻</span><span class="refresh-label">Refresh</span></button>
        <small id="refresh-status" aria-live="polite">Ready</small>
      </div>"""
        if viewer_can_refresh
        else """<div class="refresh-control">
        <small id="refresh-status" aria-live="polite">View only</small>
      </div>"""
    )
    payload_json = json.dumps(
        {
            "generated_at": payload.get("generated_at"),
            "public_data_gate": payload.get("public_data_gate"),
            "viewer_role": viewer_role,
            "can_refresh": viewer_can_refresh,
        }
    ).replace("</", "<\\/")
    summary = payload.get("combo_source_summary") or {}
    dashboard_snapshot = payload.get("dashboard_snapshot") or {}
    # Which store the snapshot came out of is an operator's question. A reader
    # is being told whether the prices are current, and naming the database
    # answers a question they did not ask in vocabulary they do not share.
    snapshot_source = (
        ("PostgreSQL collector feed" if dashboard_snapshot.get("source") == "postgres" else "Local source snapshot")
        if viewer_sees_operations
        else "Live market data"
    )
    verified_contracts = int(summary.get("verified_current_day_contract_count") or 0)
    # Only the tiers this viewer can open. Counting the research-scout tier for
    # a reader gave them a denominator for a panel that is not on their page.
    visible_slips = [primary_slip, leverage_slip, all_day_slip]
    if viewer_sees_operations:
        visible_slips.append(research_edge_slip)
    ready_tiers = sum(1 for slip in visible_slips if slip_is_built(slip))
    tier_total = len(visible_slips)
    # Built only for an operator, so a reader's page does not carry the markup
    # at all -- withholding it in CSS would still ship the worker and database
    # state to the browser, where anyone can read it.
    operator_panels_html = (
        f"""
      <section class="panel" id="sports-board">
        <div class="section-head">
          <div><span class="section-label">Operations</span><h2>Sports board · no-vig and line shopping</h2></div>
          <p>{sports_summary_text}</p>
        </div>
        {render_sports_clv_panel(sports_clv, technical=viewer_sees_diagnostics)}
        {render_sports_section(sports_board, technical=viewer_sees_diagnostics)}
      </section>

      <section class="panel" id="source-data">
        <div class="section-head">
          <div><span class="section-label">Operations</span><h2>Players, teams, and external markets</h2></div>
          <p>Fresh public-source rows. Stale rows stay hidden.</p>
        </div>
        {source_data_panel}
      </section>

      <section class="panel" id="quality">
        <div class="section-head">
          <div><span class="section-label">Operations</span><h2>Live Status</h2></div>
          <p>Stale or failed sources automatically hide review slips.</p>
        </div>
        {render_quality_panel(quality_status, public_data_gate)}
      </section>

      <section class="panel" id="record">
        <div class="section-head">
          <div><span class="section-label">Operations</span><h2>Track Record</h2></div>
          <p>Settled, resolved, and de-duplicated exposures only.</p>
        </div>
        {render_research_record_panel(research_record)}
      </section>

      <section class="panel" id="research-edge">
        <div class="section-head"><div><span class="section-label">Operations</span><h2>Research Scout Slip</h2></div><p>Research estimates remain clearly labeled</p></div>
        {render_slip_section(research_edge_slip, "RESEARCH SCOUT SLIP", "research_edge", payload, show_dollar_figures=viewer_sees_dollar_figures, analysis_report=analysis_reports.get("research_edge"))}
      </section>"""
        if viewer_sees_operations
        else ""
    )
    # The bottom bar has four slots and every one must land somewhere that
    # exists: History and Quality are operator panels now, so for a reader those
    # two links would scroll nowhere. They are replaced by the tiers a reader
    # actually came for.
    mobile_navigation_html = (
        f"""<a href="#builder">{icon("builder")}Review</a>
    <a href="#prediction-drawer">{icon("slip")}Slip</a>
    <a href="#record">{icon("record")}History</a>
    <a href="#quality">{icon("health")}Quality</a>"""
        if viewer_sees_operations
        else f"""<a href="#builder">{icon("builder")}Review</a>
    <a href="#market-browser">{icon("contracts")}Contracts</a>
    <a href="#prediction-drawer">{icon("slip")}Slip</a>
    <a href="#all-day">{icon("clock")}All-day</a>"""
    )
    operator_navigation_html = (
        """
      <a href="#sports-board">Sports</a>
      <a href="#source-data">Source data</a>
      <a href="#record">History</a>
      <a href="#quality">Quality</a>"""
        if viewer_sees_operations
        else ""
    )
    operator_sidebar_html = (
        f"""
      <div class="sidebar-section">
        <span class="sidebar-label">Operations</span>
        <nav class="side-navigation" aria-label="Operations views">
          <a href="#sports-board">{icon("sports")}<span>Sports board</span><b>{sports_summary["event_count"]}</b></a>
          <a href="#source-data">{icon("contracts")}<span>Source data</span></a>
          <a href="#research-edge">{icon("scout")}<span>Research scout</span><b>{int(research_edge_slip.get("leg_count") or 0)}</b></a>
          <a href="#quality">{icon("health")}<span>Source health</span></a>
          <a href="#record">{icon("record")}<span>Research record</span></a>
        </nav>
      </div>"""
        if viewer_sees_operations
        else ""
    )
    summary_cards_html = (
        f'<div class="stat-card"><small>Games loaded</small><strong>{len(games)}</strong></div>'
        f'<div class="stat-card"><small>Combo contracts</small><strong>{len(markets)}</strong></div>'
        if viewer_sees_operations else ""
    )
    summary_cards_html += (
        f'<div class="stat-card"><small>Contracts checked</small><strong>{verified_contracts}</strong>'
        '<span class="stat-foot">Confirmed live on Kalshi today</span></div>'
        f'<div class="stat-card {"is-accent" if ready_tiers else "is-warning"}">'
        f'<small>Review tiers ready</small><strong>{ready_tiers}/{tier_total}</strong></div>'
    )
    drawer_ready = slip_is_built(primary_slip)
    drawer_summary = (
        f'{int(primary_slip.get("leg_count") or 0)} listed legs · '
        f'{money(primary_slip.get("estimated_combo_price_cents"))}c'
        if drawer_ready else 'No verified primary slip'
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  {FAVICON_LINK}
  <meta name="theme-color" content="#080f14">
  {refresh_meta}
  <title>Hawknetic Predictions · Market Review</title>
  <link rel="stylesheet" href="{STYLESHEET.url}">
</head>
<body data-paper="{html.escape(payload_json, quote=True)}">
  <a class="skip-link" href="#primary">Skip to slips</a>
  <header class="app-topbar">
    <button class="mobile-menu-toggle" id="mobile-menu-toggle" type="button" aria-controls="app-sidebar" aria-expanded="false"><span></span><span></span><span></span><span class="sr-only">Open navigation</span></button>
    {render_brand()}
    <nav class="top-navigation" aria-label="Primary navigation">
      <a href="#builder">Review</a>
      <a href="#market-browser">Contracts</a>{operator_navigation_html}
    </nav>
    <div class="topbar-actions">
      <span class="research-only-badge"><i aria-hidden="true"></i>Research only</span>
      {refresh_control_html}
    </div>
  </header>

  <div class="sidebar-scrim" id="sidebar-scrim" hidden></div>
  <div class="app-frame">
    <aside class="app-sidebar" id="app-sidebar">
      <div class="sidebar-section">
        <span class="sidebar-label">Workspace</span>
        <nav class="side-navigation" aria-label="Review views">
          <a href="#builder">{icon("builder")}<span>Kalshi review</span></a>
          <a href="#market-browser">{icon("contracts")}<span>Live contracts</span><b>{len(markets)}</b></a>
          <a href="#primary">{icon("slip")}<span>80c+ review</span><b>{int(primary_slip.get("leg_count") or 0)}</b></a>
          <a href="#leverage">{icon("slip")}<span>75c+ review</span><b>{int(leverage_slip.get("leg_count") or 0)}</b></a>
          <a href="#all-day">{icon("clock")}<span>All-day review</span><b>{int(all_day_slip.get("leg_count") or 0)}</b></a>
        </nav>
      </div>{operator_sidebar_html}
      <div class="sidebar-live-card" data-state="{data_state}">
        <div class="live-badge{'' if data_is_ready else ' blocked'}" role="status"><i aria-hidden="true"></i><span>{data_label}</span></div>
        <strong>{generated_at_html}</strong>
        <small>{len(games)} games · {len(markets)} contracts</small>
        {data_message_html}
      </div>
      <div class="sidebar-disclaimer">
        <strong>Decision support only</strong>
        <p>No account connection, order upload, automatic trade, or guaranteed outcome.</p>
      </div>
    </aside>

    <main class="workspace">
      <section class="workspace-hero" id="builder">
        <div class="hero-top">
          <div>
            <p class="eyebrow">Live Kalshi market review</p>
            <h1>Review Kalshi markets before the slip.</h1>
            <p class="hero-tagline">Fresh market data, manual review packets, no account automation.</p>
          </div>
          <div class="workspace-meta">
            <span><small>Updated</small><strong>{generated_at_html}</strong></span>
            <span><small>Refresh cadence</small><strong>{html.escape(refresh_label)}</strong></span>
          </div>
        </div>
        <div class="alert {'is-success' if data_is_ready else 'is-warning'}" role="status">
          <span class="alert-icon" aria-hidden="true">{('✓' if data_is_ready else '!')}</span>
          <div><strong>{data_label}</strong><p>{html.escape(data_message if not data_is_ready else snapshot_source + ' is current.')}</p></div>
        </div>
        {refresh_error_html}
        <div class="stat-grid{'' if viewer_sees_operations else ' is-pair'}" role="group" aria-label="Current review summary">
          {summary_cards_html}
        </div>
      </section>

      <section class="panel" id="map">
        <div class="section-head">
          <div><span class="section-label">Review status</span><h2>Today's review slips</h2></div>
          <p>Only contracts listed on Kalshi right now, at prices quoted just now.</p>
        </div>
        {render_visual_section(payload, include_research_scout=viewer_sees_operations, show_dollar_figures=viewer_sees_dollar_figures, analysis_reports=analysis_reports)}
      </section>

      <section class="panel" id="market-browser">
        <div class="section-head">
          <div><span class="section-label">Live market</span><h2>Kalshi contracts</h2></div>
          <p>{len(markets)} combo contracts open for review right now.</p>
        </div>
        {render_market_browser(payload)}
      </section>

      <section class="panel" id="primary">
        <div class="section-head"><div><span class="section-label">Primary review</span><h2>80c+ Market Tier</h2></div><p>Higher-price exact combo legs</p></div>
        {render_slip_section(primary_slip, "80c+ MARKET TIER", "primary", payload, show_dollar_figures=viewer_sees_dollar_figures, analysis_report=analysis_reports.get("primary"))}
      </section>

      <section class="panel" id="leverage">
        <div class="section-head"><div><span class="section-label">Expanded review</span><h2>75c+ Market Tier</h2></div><p>More variance; same evidence requirements</p></div>
        {render_slip_section(leverage_slip, "75c+ MARKET TIER", "leverage", payload, show_dollar_figures=viewer_sees_dollar_figures, analysis_report=analysis_reports.get("leverage"))}
      </section>

      <section class="panel" id="all-day">
        <div class="section-head"><div><span class="section-label">All-day review</span><h2>All-Day 75-85c Tier</h2></div><p>Verified compatible contracts only</p></div>
        {render_slip_section(all_day_slip, "ALL-DAY 75-85c TIER", "all_day", payload, show_dollar_figures=viewer_sees_dollar_figures, analysis_report=analysis_reports.get("all_day"))}
      </section>
      {operator_panels_html}
    </main>

    <aside class="prediction-drawer" id="prediction-drawer" aria-labelledby="prediction-drawer-title">
      <div class="drawer-header">
        <div><span class="section-label">Current review</span><h2 id="prediction-drawer-title" tabindex="-1">Your prediction slip</h2></div>
        <a href="#primary" aria-label="Open full primary slip">↗</a>
        <button class="btn btn-tertiary btn-sm" id="close-prediction-drawer" type="button" hidden>Close slip</button>
      </div>
      {render_compact_slip(primary_slip, payload, show_dollar_figures=viewer_sees_dollar_figures, analysis_report=analysis_reports.get("primary"))}
      <div class="drawer-trust-card">
        <span aria-hidden="true">✓</span>
        <div><strong>Every leg is checked</strong><p>Each one shows its Kalshi ticker, its price, and when that price was quoted.</p></div>
      </div>
    </aside>
  </div>

  <button class="mobile-slip-toggle" id="mobile-slip-toggle" type="button" aria-expanded="false" aria-controls="prediction-drawer" hidden>
    <span class="badge {'good' if drawer_ready else 'warning'}">{'Review slip' if drawer_ready else 'Waiting'}</span>
    <span>{html.escape(drawer_summary)}</span>
    <span aria-hidden="true">↑</span>
  </button>
  <nav class="mobile-bottom-nav" aria-label="Mobile navigation">
    {mobile_navigation_html}
  </nav>
  <script src="{SCRIPT.url}" defer></script>
</body>
</html>"""


def render_slip_section(
    slip: dict,
    label: str = "COMBO SLIP",
    slip_key: str = "primary",
    source_payload: dict | None = None,
    *,
    show_dollar_figures: bool = True,
    analysis_report: dict | None = None,
) -> str:
    """One tier's slip card.

    ``show_dollar_figures`` is the operator/reader split for money. The
    estimate, the break-even and the difference between them are the research
    finding and every viewer gets them. What $5 would turn into, and the
    expected value on that $5, are the same finding multiplied by a
    hypothetical stake -- a bet-slip convention that adds no information and
    reads as one. A reader's card leaves them out; an operator's keeps them.
    """
    action = slip.get("action", "UNKNOWN")
    if action == "BUILD_SLIP" and not slip_has_authoritative_combo_evidence(slip):
        slip = {
            "action": "NO_SLIP",
            "reason": "This combination is hidden because an exact active Kalshi KXMVE listing was not verified.",
            "eligible_leg_count": 0,
        }
        action = "NO_SLIP"
    if action != "BUILD_SLIP":
        source_context = combo_source_context(source_payload, slip_key)
        source_context_html = (
            f'<p class="status-note">{html.escape(source_context)}</p>' if source_context else ""
        )
        return f"""
        <div class="slip-card empty">
          <strong>No Slip</strong>
          <p>{html.escape(slip.get("reason", "The engine did not find enough clean legs."))}</p>
          {source_context_html}
          <span>Eligible legs: {slip.get("eligible_leg_count", 0)}</span>
        </div>
        """
    grouped: dict[str, list[dict]] = {}
    for leg in slip.get("legs", []):
        grouped.setdefault(leg.get("sport", "Sports"), []).append(leg)
    sections = []
    for sport, legs in grouped.items():
        leg_items = "".join(render_slip_leg(leg) for leg in legs)
        sections.append(
            f"""
            <section class="league-block">
              <div class="league-title">
                <h3>{html.escape(sport)}</h3>
                <span>{len(legs)} {leg_label(len(legs))}</span>
              </div>
              <ul class="slip-list">{leg_items}</ul>
            </section>
            """
        )
    fallback_copy_text = slip_copy_text(slip, label)
    review_text = fallback_copy_text
    ticker_stack = ""
    analysis_html = ""
    analysis_available = False
    if slip_key in SLIP_SOURCES:
        source_payload = source_payload or {}
        slip_payload = {
            "date": source_payload.get("date"),
            "generated_at": source_payload.get("generated_at"),
            "generated_at_note": source_payload.get("generated_at_note"),
            SLIP_SOURCES[slip_key][0]: slip,
        }
        review_packet = build_review_packet(slip_payload, slip_key)
        review_text = review_packet.get("copy_blocks", {}).get("review_packet") or fallback_copy_text
        ticker_stack = review_packet.get("copy_blocks", {}).get("ticker_stack") or ""
        try:
            if analysis_report is None:
                analysis_report = build_slip_analysis(slip_payload, slip_key, stake=DEFAULT_SLIP_STAKE_DOLLARS)
            analysis_html = render_slip_analysis(analysis_report, show_dollar_figures=show_dollar_figures)
            # Set only once the block has rendered: a report that exists but
            # fails to render falls through to the unavailable block below,
            # and that card should still open on its listed contract.
            analysis_available = bool(analysis_report.get("analysis_available"))
        except Exception as error:  # noqa: BLE001 - the card must still render
            # The arithmetic block is an addition to the card, not a
            # precondition for it, so a failure here must not take the page
            # down. It is shown rather than swallowed: a card that silently
            # loses its arithmetic looks like a card that never had any.
            analysis_html = render_slip_analysis(
                {
                    "analysis_available": False,
                    "detail": f"Slip arithmetic failed to render: {type(error).__name__}: {error}",
                }
            )
    review_copy_text = html.escape(review_text, quote=True)
    ticker_copy_text = html.escape(ticker_stack, quote=True)
    packet_href = f"/review-packet.txt?slip={html.escape(slip_key, quote=True)}"
    packet_json_href = f"/review-packet.json?slip={html.escape(slip_key, quote=True)}"
    compatibility = slip.get("combo_compatibility") or {}
    compatibility_status = compatibility.get("status", "unknown")
    manual_entry_ready = compatibility.get("manual_entry_ready", slip.get("manual_entry_ready"))
    entry_status = "Ready to review" if compatibility_status == "compatible" and manual_entry_ready else "Needs review"
    combo_categories = compatibility.get("categories") or slip.get("combo_categories") or slip.get("sports") or []
    category_text = ", ".join(str(item) for item in combo_categories) or "n/a"
    max_leg_probability = slip.get("max_leg_probability")
    leg_probability_label = "Leg Range" if max_leg_probability is not None else "Leg Floor"
    leg_probability_value = (
        f"{float(slip.get('min_leg_probability') or 0) * 100:.0f}-{float(max_leg_probability) * 100:.0f}%"
        if max_leg_probability is not None
        else f"{float(slip.get('min_leg_probability') or 0) * 100:.0f}%"
    )
    combo_probability_label = "Research Estimate" if slip_key == "research_edge" else "Market implied"
    combo_chance_text, combo_chance_range = combo_probability_display(slip)
    payout_cell = (
        f'<span><small>Est. $5 Payout</small><strong>${money(slip.get("estimated_payout_if_right"))}</strong></span>'
        if show_dollar_figures
        else ""
    )
    listed_strip_html = f"""
      <div class="listed-contract">
        <span class="section-kicker">Listed contract</span>
        <div class="metric-strip">
          <span><small>{leg_probability_label}</small><strong>{leg_probability_value}</strong></span>
          <span><small>Listed combo price</small><strong>{money(slip.get("estimated_combo_price_cents"))}c</strong></span>
          <span><small>{combo_probability_label}</small><strong>{combo_chance_text}</strong>{combo_chance_range}</span>
          {payout_cell}
        </div>
      </div>"""
    # Information order for someone who is not a quant. The question a reader
    # brings to the card is "what does the model think, what does the price
    # require, and how far apart are they" -- which is the arithmetic block. The
    # listed contract's own price and the product of its leg prices are the
    # context for that answer, not the answer, so they follow it. When there is
    # no arithmetic to lead with, the card should not open on a dashed box
    # saying so; the listed figures go first and the explanation after.
    figures_html = (
        f"{analysis_html}{listed_strip_html}" if analysis_available else f"{listed_strip_html}{analysis_html}"
    )
    return f"""
    <div class="slip-card">
      <div class="slip-topline">
        <div class="slip-heading">
          <span class="section-kicker">{html.escape(label)}</span>
          <div class="slip-count"><strong>{slip.get("leg_count", 0)}</strong><span>{leg_label(slip.get("leg_count", 0))}</span></div>
          <div class="slip-review-state">
            <span class="badge {'good' if entry_status == 'Ready to review' else 'warning'}">{html.escape(entry_status)}</span>
            <span>{html.escape(category_text)}</span>
          </div>
        </div>
        <div class="packet-actions">
          <button type="button" class="btn btn-primary btn-sm copy" data-copy="{review_copy_text}">Copy Slip</button>
          <button type="button" class="btn btn-tertiary btn-sm copy" data-copy="{ticker_copy_text}">Copy Tickers</button>
          <a class="packet-download" href="{packet_href}" download>TXT</a>
          <a class="packet-download" href="{packet_json_href}" download>JSON</a>
        </div>
      </div>
      <p class="packet-note">Research packet: check price, side, and start time against Kalshi before relying on any figure here.</p>
      {figures_html}
      <div class="slip-groups">{''.join(sections)}</div>
    </div>
    """


# How a verdict reads on screen. "strong_value" is deliberately not given the
# success colour: this engine reports arithmetic on de-vigged prices, not a
# validated forecast, and a green banner would invite it to be read as one.
_VERDICT_LABELS = {
    "strong_value": ("Priced well below fair", "badge-neutral"),
    "playable": ("Slightly better than its price", "badge-neutral"),
    "thin": ("About what it costs", "badge-neutral"),
    "trim_slip": ("Priced above fair", "warning"),
    "no_realistic_path": ("No realistic path", "warning"),
    "not_priceable_from_standalone_legs": ("Not priceable from these legs", "warning"),
}

_RISK_CLASS = {"low": "badge-neutral", "moderate": "badge-neutral", "high": "warning", "very_high": "warning"}
_FRAGILITY_LABELS = {
    "low": "Few moving parts",
    "moderate": "Some fragility",
    "high": "Fragile",
    "very_high": "Very fragile",
}


def render_slip_analysis(report: dict, *, show_dollar_figures: bool = True) -> str:
    """The arithmetic block on a slip card.

    Renders the refusals as prominently as the numbers. A slip analysed on three
    of five legs, or one whose quotes went stale, produces a figure that looks
    exactly as confident as a complete one, so the count and the reasons are on
    the card rather than only in the JSON.

    With ``show_dollar_figures`` off the expected-value cell is left out. EV
    here is the difference between the estimate and the break-even, multiplied
    by a hypothetical $5 -- the same finding the strip already shows in
    probability points, restated as money on a stake. That restatement is the
    one thing on the card a sportsbook would also print, and it is the figure
    most likely to be read as advice, so a reader's card does without it.
    """

    if not report.get("analysis_available"):
        return f"""
        <div class="slip-analysis unavailable">
          <span class="section-kicker">Estimate vs. price</span>
          <p class="status-note">{html.escape(str(report.get("detail") or "No analysis available for this slip."))}</p>
        </div>
        """

    analysis = report["analysis"]
    hit = float(analysis["hit_probability"])
    break_even = float(analysis["break_even_probability"])
    edge = float(analysis["edge_over_break_even"])
    achievable = bool(analysis["expected_value_is_achievable"])
    verdict_label, verdict_class = _VERDICT_LABELS.get(
        analysis["verdict"], (str(analysis["verdict"]).replace("_", " "), "badge-neutral")
    )
    risk = str(analysis["risk_tier"])
    precision = str(analysis["precision"])

    # "Needs to hit" is exact arithmetic on the leg prices, so it keeps two
    # decimals. "Estimated to hit" is a simulation, and the difference is that
    # simulation minus an exact number -- so the difference is uncertain by
    # exactly as much as the estimate, and the two have to be quoted alike. The
    # card used to print all three at two decimals and then, in a note below,
    # concede that the estimate did not support two decimals.
    interval = analysis.get("hit_probability_interval")
    hit_decimals = significant_decimals(interval)
    hit_range = (
        f'<small class="metric-range">95% CI '
        f"{float(interval[0]) * 100:.{hit_decimals}f}-{float(interval[1]) * 100:.{hit_decimals}f}%</small>"
        if interval
        else ""
    )

    # The break-even here comes from the individual leg prices, while the
    # listed-contract strip beside this block shows the combo contract's own
    # price. They are different instruments and legitimately differ, but sitting
    # adjacent and unlabelled they read as the page contradicting itself.
    notes = [
        "Break-even is computed from the individual leg prices; the listed combo contract can be priced above or below them.",
        "Fragility reflects leg count, correlation, and estimated hit probability.",
    ]
    skipped = report.get("skipped_legs") or []
    if skipped:
        reasons = ", ".join(f'{item["leg_id"]} ({item["reason"]})' for item in skipped)
        notes.append(
            f'Analysed {report["priced_leg_count"]} of {report["submitted_leg_count"]} legs. '
            f"Excluded: {reasons}."
        )
    if not achievable:
        warning = analysis.get("same_event_repricing_warning") or {}
        notes.append(str(warning.get("detail") or "This slip's expected value is not takeable at these prices."))
    if precision == "exact":
        notes.append(
            "These legs share nothing the correlation model recognises, so the "
            "joint probability is the exact product rather than a simulation."
        )
    elif precision != "good":
        notes.append(
            f"Simulation precision is {precision.replace('_', ' ')}; the hit "
            "probability is quoted only to the digits its interval supports."
        )

    note_html = "".join(f'<p class="status-note">{html.escape(note)}</p>' for note in notes)
    # Expected value is the estimate times the payout, so it inherits the
    # estimate's uncertainty undiluted -- and it is the figure on this card most
    # likely to be acted on. On a long slip the payout multiplier is in the
    # thousands, which turns a tenth of a point of simulation error into dollars:
    # a "$3.45" quoted alone from a band running $1.63 to $5.28 is the card's
    # most confident number resting on its least certain one. EV rises with the
    # hit probability and nothing else here moves, so the interval carries
    # straight through the same arithmetic.
    ev_range = ""
    if interval:
        payout = float(analysis["payout_if_won"])
        stake_value = float(analysis["stake"])
        low_ev = float(interval[0]) * payout - stake_value
        high_ev = float(interval[1]) * payout - stake_value
        # Below a cent the band adds nothing a reader can use, and an exact
        # analysis has no band at all.
        if high_ev - low_ev >= 0.01:
            ev_range = f'<small class="metric-range">95% CI {dollars(low_ev)} to {dollars(high_ev)}</small>'
    if not show_dollar_figures:
        ev_cell = ""
    elif achievable:
        ev_cell = (
            f'<span><small>EV on ${money(analysis["stake"])}</small>'
            f'<strong>{dollars(analysis["expected_value"])}</strong>{ev_range}</span>'
        )
    else:
        ev_cell = '<span><small>EV</small><strong class="withheld">withheld</strong></span>'
    return f"""
    <div class="slip-analysis">
      <div class="slip-analysis-head">
        <span class="section-kicker">Estimate vs. price</span>
        <div class="slip-analysis-badges">
          <span class="badge {verdict_class}">{html.escape(verdict_label)}</span>
          <span class="badge {_RISK_CLASS.get(risk, "badge-neutral")}">{_FRAGILITY_LABELS.get(risk, "Fragility unknown")}</span>
        </div>
      </div>
      <div class="metric-strip">
        <span><small>Estimated to hit</small><strong>{hit * 100:.{hit_decimals}f}%</strong>{hit_range}</span>
        <span><small>Needs to hit</small><strong>{break_even * 100:.2f}%</strong></span>
        <span class="{'delta-up' if edge > 0 else 'delta-down'}"><small>Difference</small><strong>{probability_points(edge, hit_decimals)}</strong></span>
        {ev_cell}
      </div>
      {note_html}
      {render_leg_breakdown(analysis)}
    </div>
    """


# Edge thresholds in probability points, and the label each one earns. A leg is
# only ever described by how its price compares to its probability.
_LEG_EDGE_FLAGS = (
    (0.02, "Good cushion", "badge-success"),
    (0.0, "Thin edge", "badge-warning"),
)


def leg_edge_flag(edge: float) -> tuple[str, str]:
    """Label one leg's edge.

    The edge is rounded before comparison: it is a difference of two decimal
    quantities, so 0.86-0.84 and 0.84-0.82 are the same two points, and in
    binary floating point they straddle the threshold. Without the rounding the
    same difference gets two different labels depending on which leg it came
    from.
    """
    settled = round(edge, 9)
    if settled < 0:
        return "Priced over", "badge-danger"
    if settled == 0:
        return "No edge", "badge-neutral"
    for threshold, label, css_class in _LEG_EDGE_FLAGS:
        if settled >= threshold:
            return label, css_class
    return "Thin edge", "badge-warning"


def render_leg_breakdown(analysis: dict) -> str:
    """Per-leg price-versus-probability, which the engine computes and the card never showed.

    Every figure here already existed in the analysis payload. The slip-level
    numbers alone cannot say *which* leg is carrying the slip and which is
    dragging it, and that is the question anyone trimming a combo is asking.

    Price and break-even share a column because on these contracts they are the
    same number: the engine takes ``decimal_odds = 100 / ask_cents``, so
    ``break_even = 1 / decimal_odds`` is just ``ask_cents / 100``. Printing both
    spent a column -- the scarcest thing on a phone -- to say one thing twice,
    and invited the reader to look for a relationship between two figures that
    are identical by construction.
    """
    legs = list(analysis.get("legs") or [])
    if not legs:
        return ""
    rows = []
    for leg in legs:
        edge = float(leg.get("edge") or 0.0)
        label, css_class = leg_edge_flag(edge)
        break_even = float(leg.get("break_even") or 0.0)
        fair = float(leg.get("fair_probability") or 0.0)
        selection = str(leg.get("selection") or leg.get("leg_id") or "Leg")
        rows.append(
            f"""
            <tr>
              <th scope="row">{html.escape(selection)}<small>{html.escape(str(leg.get("league") or ""))}</small></th>
              <td data-label="Ask / break-even">{break_even * 100:.1f}c</td>
              <td data-label="Estimated">{fair * 100:.1f}%</td>
              <td data-label="Difference" class="{'delta-up' if round(edge, 9) > 0 else 'delta-down'}">{probability_points(edge)}</td>
              <td data-label="Read"><span class="badge {css_class}">{html.escape(label)}</span></td>
            </tr>
            """
        )
    return f"""
    <details class="leg-breakdown">
      <summary>Leg breakdown ({len(legs)})</summary>
      <div class="leg-breakdown-scroll">
        <table>
          <caption>Each leg's estimated probability against the break-even its price implies. On a
          binary contract the ask in cents <em>is</em> that break-even, so 84.0c means 84.0%.</caption>
          <thead>
            <tr><th scope="col">Pick</th><th scope="col">Ask / break-even</th><th scope="col">Estimated</th><th scope="col">Difference</th><th scope="col">Read</th></tr>
          </thead>
          <tbody>{''.join(rows)}</tbody>
        </table>
      </div>
    </details>
    """


def render_slip_leg(leg: dict) -> str:
    label = leg.get("subtitle") or leg.get("title") or leg.get("market_ticker", "")
    event = leg.get("display_event") or leg.get("event_ticker") or ""
    ticker = leg.get("market_ticker") or ""
    status = leg.get("status") or "n/a"
    category = leg.get("combo_category") or leg.get("category") or leg.get("sport") or "n/a"
    start_time = leg.get("event_start_time") or ""
    close_time = leg.get("market_close_time") or leg.get("close_time") or ""
    start_time_text = display_event_time(start_time)
    close_time_text = display_event_time(close_time)
    probability = float(leg.get("probability") or 0) * 100.0
    required = float(leg.get("required_probability") or 0) * 100.0
    side = html.escape(leg.get("side", "").upper())
    ask = money(leg.get("ask_cents"))
    if leg.get("research_probability") is not None:
        probability_kind = "Research estimate"
        margin = float(leg.get("margin_of_error") or 0) * 100.0
        kalshi = float(leg.get("kalshi_probability") or 0) * 100.0
        evidence_count = int(leg.get("evidence_count") or 0)
        detail_rows = [
            ("Kalshi", f"{kalshi:.1f}%"),
            ("Margin", f"+/-{margin:.1f}%"),
            ("Sources", str(evidence_count)),
        ]
    else:
        probability_kind = "Market implied"
        detail_rows = [("Floor", f"{required:.0f}%")]
    detail_rows.extend(
        [
            ("Category", str(category)),
            ("Status", str(status)),
            ("Closes", close_time_text),
        ]
    )
    detail_html = "".join(
        f"<div><dt>{html.escape(name)}</dt><dd>{html.escape(value)}</dd></div>" for name, value in detail_rows
    )
    return (
        f"<li class=\"slip-leg\">"
        f"<div class=\"leg-copy\"><strong>{html.escape(event)}</strong><span>{side} / {html.escape(label)}</span>"
        f"<div class=\"leg-chips\"><time datetime=\"{html.escape(str(start_time), quote=True)}\">{html.escape(start_time_text)}</time>"
        f"<span>{ask}c ask</span></div></div>"
        f"<div class=\"leg-metrics\"><b>{probability:.1f}%</b><small>{probability_kind}</small></div>"
        f"<details class=\"leg-details\"><summary>Market details</summary><code>{html.escape(ticker)}</code><dl>{detail_html}</dl></details>"
        f"</li>"
    )


def render_visual_section(
    payload: dict, *, include_research_scout: bool = True, show_dollar_figures: bool = True,
    analysis_reports: dict | None = None,
) -> str:
    """The tier summary, over the tiers the viewer can actually open.

    The research-scout tier is operator-only, so summarising it for a reader
    advertises a panel that is not on their page: a card that cannot be reached
    and a denominator counting something invisible ("2/4" where only three
    exist). The count is derived from this list rather than written as a
    literal, so gating a tier cannot leave the total behind again.
    """
    tiers = [
        ("80c+ Market", "primary", payload.get("custom_slip") or {}),
        ("75c+ Market", "leverage", payload.get("leverage_slip") or {}),
        ("All-Day 75-85c", "all_day", payload.get("all_day_slip") or {}),
    ]
    if include_research_scout:
        tiers.append(
            ("Research Scout", "research_edge", payload.get("research_edge_slip") or {})
        )
    cards = []
    built_count = 0
    total_legs = 0
    source_ready = (payload.get("public_data_gate") or {}).get("status") == "ready"
    source_context = combo_source_context(payload)
    for name, slip_key, slip in tiers:
        is_built = slip_is_built(slip)
        if is_built:
            built_count += 1
        payout = float(slip.get("estimated_payout_if_right") or 0) if is_built else 0.0
        legs = int(slip.get("leg_count") or 0) if is_built else 0
        total_legs += legs
        headline = str(legs) if is_built else "-"
        # One line on a small tile, so the band does not fit -- but the figure
        # must not claim two decimals it has not got just because there is no
        # room to qualify it. The card beside this one carries the interval.
        report = (analysis_reports or {}).get(slip_key)
        if report is None:
            report = slip_analysis_report(slip, payload, slip_key)
        chance_text, _ = slip_estimate_display(report)
        subline = (
            f"Estimated to hit {chance_text}"
            if is_built
            else ("No qualifying legs" if source_ready else "Waiting for fresh data")
        )
        # The tile's second line is the return on $5 for an operator and the
        # listed price for a reader: the price is the number a reader will go
        # and check on Kalshi, and it carries no wagering framing.
        if not is_built:
            payout_text = "Unavailable"
        elif show_dollar_figures:
            payout_text = f"Est. ${money(payout)}"
        else:
            payout_text = f"Listed {money(slip.get('estimated_combo_price_cents'))}c"
        status_text = "Ready" if is_built else ("No slip" if source_ready else "Blocked")
        status_badge = "badge-success" if is_built else "badge-warning"
        cards.append(
            f"""
            <article class="tier-card{' is-ready' if is_built else ''}">
              <div class="tier-head">
                <span>{html.escape(name)}</span>
                <span class="badge {status_badge}">{status_text}</span>
              </div>
              <div class="tier-count"><strong>{headline}</strong><em>{leg_label(headline)}</em></div>
              <div class="tier-meta">
                <small>{subline}</small>
                <small>{payout_text}</small>
              </div>
            </article>
            """
        )
    generated_at_html = timestamp_element(payload.get("generated_at"))
    return f"""
    <div class="tier-grid-wrap">
      <div class="ready-summary{' is-blocked' if not built_count else ''}" role="group" aria-labelledby="ready-summary-label">
        <span class="section-kicker" id="ready-summary-label">Ready tiers</span>
        <span class="ready-count">{built_count}/{len(tiers)}</span>
        <small>{total_legs} {leg_label(total_legs)} across ready slips · updated {generated_at_html}</small>
        {f'<p class="status-note">{html.escape(source_context)}</p>' if source_context else ''}
      </div>
      <div class="tier-grid">{''.join(cards)}</div>
    </div>
    """


def combo_source_context(source_payload: dict | None, slip_key: str | None = None) -> str:
    summary = (source_payload or {}).get("combo_source_summary") or {}
    active_count = int(summary.get("active_kxmve_market_count") or 0)
    verified_count = int(summary.get("verified_current_day_contract_count") or 0)
    if not active_count:
        return ""
    # The counts are worth showing -- they say how much of today's board got
    # through -- but "active KXMVE contracts with complete exact-contract
    # evidence" describes the check, in the ticker prefix and the vocabulary of
    # the code that runs it. A reader wants the same two numbers in words they
    # already use.
    base = (
        f"Kalshi has {active_count} combo contracts open; "
        f"{verified_count} are confirmed for today's games."
    )
    if not slip_key:
        return base
    tier = (summary.get("tiers") or {}).get(slip_key) or {}
    eligible_count = int(tier.get("eligible_exact_combo_count") or 0)
    if eligible_count:
        return f"{base} {eligible_count} fit this tier."
    return f"{base} None fit this tier, so there is nothing to show here."


def source_age_text(age: object) -> str:
    """How old the dashboard payload is, or that nobody knows.

    Absence used to render as the word "Fresh". `build_source_quality_report`
    sets `data_age_seconds` to None exactly when `generated_at` is missing or
    will not parse -- it records `missing_or_invalid_generated_at` and deducts
    twenty points from the quality score for that same condition. The one state
    in which the platform knows least about freshness was the one it described
    with its strongest word for freshness, and the panel could read "Review
    blocked" beside "Fresh" in the same heading.

    Unknown is its own answer, and it is not the same answer as `0s old`, which
    is a measured age of zero.

    Days exist because hours had no ceiling: a payload stopped in 2000 read
    `233712h old`, a number no reader converts. A non-numeric age reports
    unknown rather than raising, because this text is drawn on the operations
    page and an unparseable field should not take the page down with it.
    """

    # `age in {None, ""}` was the obvious way to write this and it hashes what
    # it is handed, so a list or a dict raised TypeError before reaching the
    # fallback below -- the one line meant to keep bad metadata off this page
    # was itself a way for bad metadata to take it down.
    if age is None or (isinstance(age, str) and not age):
        return "Age unknown"
    try:
        age_seconds = max(0, int(float(age)))  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        # OverflowError, not just the parse failures: `int(float("inf"))` and
        # `float(10 ** 400)` both raise it, and neither is a TypeError or a
        # ValueError.
        return "Age unknown"
    if age_seconds < 60:
        return f"{age_seconds}s old"
    if age_seconds < 3600:
        return f"{age_seconds // 60}m old"
    if age_seconds < 86400:
        return f"{age_seconds // 3600}h old"
    days = age_seconds // 86400
    return f"{days}d old"


def render_quality_panel(status: dict, public_data_gate: dict | None = None) -> str:
    gate = status.get("source_quality_gate") or {}
    public_data_gate = public_data_gate or {}
    slip_counts = gate.get("slip_counts") or status.get("slip_counts") or {}
    data_is_ready = public_data_gate.get("status") == "ready"
    decision_class = "good" if data_is_ready else "warning"
    primary = int(slip_counts.get("primary") or 0)
    leverage = int(slip_counts.get("leverage") or 0)
    all_day = int(slip_counts.get("all_day") or 0)
    research_edge = int(slip_counts.get("research_edge") or 0)
    age_text = source_age_text(status.get("data_age_seconds"))
    public_status = "Fresh data" if data_is_ready else "Review blocked"
    gate_message = str(public_data_gate.get("message") or "Fresh data is required before review.")
    gate_message_html = "" if data_is_ready else f'<p class="status-note">{html.escape(gate_message)}</p>'
    return f"""
    <div class="decision {decision_class}">
      <div class="status-heading"><strong>{html.escape(public_status)}</strong><span>{html.escape(str(age_text))}</span></div>
      {gate_message_html}
      <div class="metric-strip">
        <span><small>80c+</small><strong>{primary}</strong></span>
        <span><small>75c+</small><strong>{leverage}</strong></span>
        <span><small>All-Day</small><strong>{all_day}</strong></span>
        <span><small>Scout</small><strong>{research_edge}</strong></span>
      </div>
    </div>
    """


def render_research_record_panel(record: dict) -> str:
    tracks = record.get("tracks") or []
    track_cards = "".join(render_research_record_track(track) for track in tracks) or """
      <div class="decision warning">
        <strong>No record yet</strong>
        <p>No settled rows are available yet. Keep collecting before showing hit-rate metrics.</p>
      </div>
    """
    status_label = str(record.get("status") or "WATCH")
    decision_class = "good" if status_label == "OK" else "warning"
    return f"""
    <div class="decision {decision_class}">
      <div class="record-heading"><span class="badge {decision_class}">{html.escape(status_label)}</span><span>Settled + de-duped</span></div>
      <div class="record-grid">{track_cards}</div>
    </div>
    """


def render_research_record_track(track: dict) -> str:
    hit_rate = track.get("observed_hit_rate")
    raw_hit_rate = track.get("observed_hit_rate_raw")
    # The state class carries the colour. Without it every value took the
    # success accent, so "Unavailable" and "Pending" rendered in the same green
    # as a real measured rate -- an absent number wearing the colour of a good
    # one, on a platform whose whole point is not to present absence as a result.
    interval = track.get("observed_hit_rate_interval")
    if hit_rate is not None:
        # One decimal, not two. The interval below carries the precision, and a
        # second decimal on a figure whose 95% band spans several points is a
        # claim the sample cannot support.
        hit_rate_text = f"{float(hit_rate) * 100:.1f}%"
        hit_rate_status = (
            f"95% CI {float(interval[0]) * 100:.0f}-{float(interval[1]) * 100:.0f}% "
            f"on {int(track.get('win_loss_count') or 0)} settled"
            if interval
            else "Settled sample"
        )
        hit_rate_state = "is-measured"
    elif raw_hit_rate is not None:
        hit_rate_text = "Pending"
        hit_rate_status = "More data needed"
        hit_rate_state = "is-pending"
    else:
        hit_rate_text = "Unavailable"
        hit_rate_status = "No settled rows"
        hit_rate_state = "is-absent"
    return f"""
      <article class="record-card">
        <div class="card-head">
          <h3>{html.escape(str(track.get("bot_name", "")))}</h3>
          <span class="badge badge-neutral">research</span>
        </div>
        <div class="record-rate {hit_rate_state}"><small>Hit rate</small><strong>{html.escape(hit_rate_text)}</strong><span>{html.escape(hit_rate_status)}</span></div>
        <div class="metric-strip">
          <span><small>Valid</small><strong>{int(track.get("valid_rows") or 0)}</strong></span>
          <span><small>Settled</small><strong>{int(track.get("settled_rows") or 0)}</strong></span>
          <span><small>Unique</small><strong>{int(track.get("deduped_settled_exposures") or 0)}</strong></span>
          <span><small>Open</small><strong>{int(track.get("unresolved_rows") or 0)}</strong></span>
        </div>
      </article>
    """


class PaperHandler(BaseHTTPRequestHandler):
    server_version = "HawkNeticResearch"
    sys_version = ""
    data_path = repo_path("data", "today_paper_view.json")
    audit_path = repo_path("data", "refresh_audit.jsonl")
    error_path = repo_path("data", "error_events.jsonl")
    refresh_seconds = 0
    refresh_config: dict = {}
    refresh_lock = threading.Lock()
    last_manual_refresh_at = 0.0
    refresh_status = {
        "state": "idle",
        "message": "Ready. Pulls fresh market data and rebuilds the slips.",
    }

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if path.startswith("/assets/"):
            # Styles, script, and fonts carry no data and are needed by the
            # sign-in page itself, so they are served before authentication.
            self.send_asset(path)
            return
        if path == "/login":
            if not user_auth_enabled():
                self.send_error(404)
                return
            self.send_html(render_login_page())
            return
        if path == "/healthz":
            self.send_json({"status": "ok", "service": "kalshi-research-dashboard"})
            return
        if path == "/readyz":
            readiness = build_service_readiness(load_current_payload(self.data_path))
            self.send_json(readiness, status_code=200 if readiness["status"] == "ready" else 503)
            return
        if path == "/internal/status.json":
            if not self.authorize_request(required_role="admin"):
                return
            self.send_json(build_internal_status())
            return
        if not self.authorize_request(required_role="read_only"):
            return
        if path == "/auth/me":
            self.send_json(
                {
                    "username": self.principal.username,
                    "role": self.principal.role,
                    "auth_method": self.principal.auth_method,
                    "session_expires_at": self.principal.session_expires_at,
                }
            )
            return
        if path == "/ops":
            if not self.require_role("admin"):
                return
            self.send_html(render_operator_page())
            return
        if path == "/internal/operator-messages.json":
            if not self.require_role("admin"):
                return
            inbox = self.operator_inbox
            if inbox is None:
                self.send_json({"error": "operator_inbox_unavailable"}, status_code=503)
                return
            self.send_json({"counts": inbox.counts(), "messages": inbox.list(limit=200)})
            return
        payload = load_current_payload(self.data_path)
        safe_payload = safe_dashboard_payload(payload)
        if path in {"/", "/index.html"}:
            try:
                source_data_preview = build_source_data_preview()
            except Exception:
                source_data_preview = {}
            refreshed_csrf_cookie = self.reissued_csrf_cookie()
            self.send_html(
                render_dashboard(
                    safe_payload,
                    self.refresh_seconds,
                    principal=getattr(self, "principal", None),
                    source_data_preview=source_data_preview,
                ),
                extra_cookies=[refreshed_csrf_cookie] if refreshed_csrf_cookie else None,
            )
            return
        if path == "/data.json":
            self.send_json(
                consumer_payload(
                    safe_payload,
                    include_research_scout=role_allows(self.viewer_role(), "admin"),
                )
            )
            return
        if path == "/api/v1":
            self.send_json(build_data_catalog_payload(safe_payload, viewer_role=self.viewer_role()))
            return
        if path == "/api/v1/source-data":
            if not self.require_role("admin"):
                return
            try:
                self.send_json(SourceDataStore().summary())
            except Exception as exc:
                self.send_json(
                    {"status": "blocked", "error": f"source_data_unavailable:{type(exc).__name__}"},
                    status_code=503,
                )
            return
        if path == "/api/v1/source-data/entities":
            if not self.require_role("admin"):
                return
            try:
                self.send_json(
                    SourceDataStore().list_entities(
                        source=_query_value(query, "source"),
                        entity_type=_query_value(query, "type"),
                        competition=_query_value(query, "competition"),
                        search=_query_value(query, "search"),
                        limit=_query_integer(
                            query, "limit", 50, minimum=1, maximum=MAX_DETAIL_PAGE_SIZE
                        ),
                        offset=_query_integer(
                            query, "offset", 0, minimum=0, maximum=1_000_000
                        ),
                        max_age_seconds=_query_integer(
                            query, "max_age_seconds", 43200, minimum=60, maximum=604800
                        ),
                    )
                )
            except Exception as exc:
                self.send_json(
                    {"status": "blocked", "error": f"source_entities_unavailable:{type(exc).__name__}"},
                    status_code=503,
                )
            return
        if path == "/api/v1/source-data/markets":
            if not self.require_role("admin"):
                return
            try:
                self.send_json(
                    SourceDataStore().list_external_markets(
                        venue=_query_value(query, "venue") or "polymarket",
                        market_type=_query_value(query, "market_type"),
                        search=_query_value(query, "search"),
                        starts_within_hours=(
                            _query_integer(
                                query, "starts_within_hours", 24, minimum=1, maximum=168
                            )
                            if _query_value(query, "starts_within_hours")
                            else None
                        ),
                        limit=_query_integer(
                            query, "limit", 50, minimum=1, maximum=MAX_DETAIL_PAGE_SIZE
                        ),
                        offset=_query_integer(
                            query, "offset", 0, minimum=0, maximum=1_000_000
                        ),
                        max_age_seconds=_query_integer(
                            query, "max_age_seconds", 7200, minimum=60, maximum=86400
                        ),
                    )
                )
            except Exception as exc:
                self.send_json(
                    {"status": "blocked", "error": f"external_markets_unavailable:{type(exc).__name__}"},
                    status_code=503,
                )
            return
        if path == "/api/v1/source-data/live":
            if not self.require_role("admin"):
                return
            try:
                self.send_json(
                    SourceDataStore().list_live_data(
                        competition=_query_value(query, "competition"),
                        limit=_query_integer(
                            query, "limit", 50, minimum=1, maximum=MAX_DETAIL_PAGE_SIZE
                        ),
                        max_age_seconds=_query_integer(
                            query, "max_age_seconds", 900, minimum=60, maximum=86400
                        ),
                    )
                )
            except Exception as exc:
                self.send_json(
                    {"status": "blocked", "error": f"live_source_data_unavailable:{type(exc).__name__}"},
                    status_code=503,
                )
            return
        if path.startswith("/api/v1/source-data/refresh/"):
            if not self.require_role("admin"):
                return
            request_id = path.removeprefix("/api/v1/source-data/refresh/").strip()
            try:
                refresh = SourceDataStore().get_refresh(request_id)
            except Exception as exc:
                self.send_json(
                    {"status": "blocked", "error": f"source_refresh_unavailable:{type(exc).__name__}"},
                    status_code=503,
                )
                return
            if refresh is None:
                self.send_json({"error": "source_refresh_not_found"}, status_code=404)
            else:
                self.send_json(refresh)
            return
        if path in {"/games.json", "/api/v1/games"}:
            self.send_json(build_detail_collection_payload(safe_payload, "games", query))
            return
        if path in {"/markets.json", "/api/v1/markets"}:
            self.send_json(build_detail_collection_payload(safe_payload, "markets", query))
            return
        if path == "/sports.json":
            if not self.require_role("admin"):
                return
            board = safe_sports_board()
            self.send_json(
                board if (query.get("detail") or ["full"])[0] == "full" else summarize_sports_board(board)
            )
            return
        if path == "/sports-clv.json":
            if not self.require_role("admin"):
                return
            self.send_json(safe_sports_clv_report())
            return
        if path == "/review-packets.json":
            if not self.require_role("researcher"):
                return
            self.send_json(build_all_review_packets(safe_payload))
            return
        if path == "/review-packet.json":
            if not self.require_role("researcher"):
                return
            slip_key = (query.get("slip") or ["primary"])[0]
            try:
                packet = build_review_packet(safe_payload, slip_key)
            except ValueError as exc:
                self.send_json({"error": str(exc), "valid_slips": sorted(SLIP_SOURCES)}, status_code=400)
                return
            self.send_json(packet)
            return
        if path == "/slip-analysis.json":
            if not self.require_role("researcher"):
                return
            slip_key = (query.get("slip") or ["primary"])[0]
            raw_stake = (query.get("stake") or ["1"])[0]
            try:
                stake = float(raw_stake)
            except (TypeError, ValueError):
                self.send_json({"error": f"stake_not_a_number:{raw_stake}"}, status_code=400)
                return
            try:
                self.send_json(build_slip_analysis(safe_payload, slip_key, stake=stake))
            except ValueError as exc:
                self.send_json(
                    {"error": str(exc), "valid_slips": sorted(SLIP_SOURCES)}, status_code=400
                )
            return
        if path == "/review-packet.txt":
            if not self.require_role("researcher"):
                return
            slip_key = (query.get("slip") or ["primary"])[0]
            try:
                packet = build_review_packet(safe_payload, slip_key)
            except ValueError as exc:
                self.send_json({"error": str(exc), "valid_slips": sorted(SLIP_SOURCES)}, status_code=400)
                return
            self.send_text(
                render_review_packet_text(packet),
                filename=safe_review_packet_filename(packet, "txt"),
            )
            return
        if path == "/refresh-status":
            self.send_json(dict(self.refresh_status))
            return
        if path == "/freshness.json":
            # Every signed-in role needs to know the snapshot moved on; only
            # an admin may act on it, which /quality.json enforces separately.
            gate = safe_payload.get("public_data_gate") or {}
            self.send_json(
                {
                    "generated_at": safe_payload.get("generated_at"),
                    "status": gate.get("status"),
                    "code": gate.get("code"),
                    "data_age_seconds": gate.get("data_age_seconds"),
                    # The gate writes a different sentence for each way it
                    # blocks -- a missing timestamp is not a stale source is not
                    # an ordinary stale payload. The poller had only a generic
                    # "Data is stale" to show, so a reader was told the one
                    # thing that happened to be least often true.
                    "message": gate.get("message"),
                }
            )
            return
        if path == "/quality.json":
            if not self.require_role("admin"):
                return
            self.send_json(build_quality_status(payload, self.audit_path, self.error_path))
            return
        if path == "/research-record.json":
            if not self.require_role("admin"):
                return
            self.send_json(build_research_record(payload=payload))
            return
        self.send_error(404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/auth/login":
            self.handle_login()
            return
        if not self.authorize_request(required_role="read_only"):
            return
        if path == "/auth/logout":
            self.handle_logout()
            return
        if path == "/internal/operator-messages":
            if not self.require_role("admin"):
                return
            if not valid_research_action(self.headers, OPERATOR_ACTION_VALUE) or not valid_json_content_type(
                self.headers
            ):
                self.send_json({"error": "operator_message_request_rejected"}, status_code=403)
                return
            if not self.valid_session_csrf():
                self.send_json({"error": "csrf_validation_failed"}, status_code=403)
                return
            self.handle_operator_message()
            return
        if path == "/api/v1/source-data/refresh":
            if not self.require_role("admin"):
                return
            if not valid_research_action(self.headers, SOURCE_REFRESH_ACTION_VALUE) or not valid_json_content_type(
                self.headers
            ):
                self.send_json({"error": "source_refresh_request_rejected"}, status_code=403)
                return
            if not self.valid_session_csrf():
                self.send_json({"error": "csrf_validation_failed"}, status_code=403)
                return
            self.handle_source_refresh()
            return
        if path == "/refresh":
            if not self.require_role("admin"):
                return
            if not valid_refresh_action(self.headers):
                self.send_json(
                    {"state": "rejected", "message": "Refresh request was rejected."},
                    status_code=403,
                )
                return
            if not self.valid_session_csrf():
                self.send_json(
                    {"state": "rejected", "message": "Session CSRF validation failed."},
                    status_code=403,
                )
                return
            status = self.run_refresh(reason="manual", async_run=True)
            status_code = 202 if status.get("accepted") else int(status.get("status_code", 409))
            self.send_json(status, status_code=status_code)
            return
        self.send_error(404)

    @property
    def auth_store(self) -> LocalAuthStore | None:
        if not user_auth_enabled():
            return None
        try:
            return LocalAuthStore()
        except Exception:
            return None

    @property
    def operator_inbox(self) -> OperatorInbox | None:
        try:
            return OperatorInbox()
        except Exception:
            return None

    def authorize_request(self, *, required_role: str = "read_only") -> bool:
        principal = authenticate_dashboard_request(
            self.headers.get("Authorization"),
            self.headers.get("Cookie"),
            auth_store=self.auth_store,
        )
        if principal is not None and role_allows(principal.role, required_role):
            self.principal = principal
            return True
        if principal is not None:
            self.send_json({"error": "role_forbidden"}, status_code=403)
            return False
        configuration_missing = dashboard_auth_enabled() and not dashboard_auth_configured()
        accepts_html = "text/html" in str(self.headers.get("Accept") or "")
        if user_auth_enabled() and accepts_html and not configuration_missing:
            self.send_response(302)
            self.send_header("Location", "/login")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return False
        body = b"Dashboard authentication is not configured." if configuration_missing else b"Authentication required."
        self.send_response(503 if configuration_missing else 401)
        self.send_header("WWW-Authenticate", 'Basic realm="HawkNetic Research Dashboard"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return False

    def viewer_role(self) -> str:
        """This request's role, defaulting to the least privilege.

        Reaching for `principal.role` inline meant every caller had to
        remember what an absent principal means; getting that wrong is how a
        default becomes admin by accident.
        """
        return str(getattr(getattr(self, "principal", None), "role", "") or "read_only")

    def require_role(self, required_role: str) -> bool:
        principal = getattr(self, "principal", None)
        if principal is not None and role_allows(principal.role, required_role):
            return True
        self.send_json({"error": "role_forbidden", "required_role": required_role}, status_code=403)
        return False

    def reissued_csrf_cookie(self) -> str | None:
        """Mint a CSRF cookie for a session that arrived without one.

        A session outlives the single copy of its CSRF token handed out at
        sign-in, so a second tab or a restarted browser would otherwise hold a
        valid session that can no longer post anything. Re-issuing on the page
        that carries the script keeps that recovery invisible.
        """
        principal = getattr(self, "principal", None)
        if principal is None or principal.auth_method != "session":
            return None
        if csrf_token_from_cookie(self.headers.get("Cookie")):
            return None
        store = self.auth_store
        session_token = session_token_from_cookie(self.headers.get("Cookie"))
        if store is None or not session_token:
            return None
        csrf_token = store.rotate_csrf(session_token)
        if not csrf_token:
            return None
        return build_csrf_cookie(csrf_token, secure=hosted_runtime())

    def valid_session_csrf(self) -> bool:
        principal = getattr(self, "principal", None)
        if principal is None or principal.auth_method != "session":
            return True
        store = self.auth_store
        token = session_token_from_cookie(self.headers.get("Cookie"))
        return bool(store and store.validate_csrf(token or "", self.headers.get("X-CSRF-Token")))

    def handle_login(self) -> None:
        store = self.auth_store
        if store is None:
            self.send_json({"error": "user_auth_unconfigured"}, status_code=503)
            return
        try:
            content_length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            content_length = 0
        if content_length <= 0 or content_length > 4096:
            self.send_json({"error": "invalid_login_payload"}, status_code=400)
            return
        try:
            payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_json({"error": "invalid_login_payload"}, status_code=400)
            return
        principal = store.authenticate_password(
            str(payload.get("username") or ""),
            str(payload.get("password") or ""),
            remote_address=self.client_address[0] if self.client_address else None,
            user_agent=self.headers.get("User-Agent"),
            maximum_failures=max(1, _env_int("AUTH_MAX_FAILED_LOGINS", 5)),
            lock_minutes=max(1, _env_int("AUTH_LOCK_MINUTES", 15)),
        )
        if principal is None:
            self.send_json({"error": "invalid_credentials"}, status_code=401)
            return
        session_token, session_principal = store.create_session(
            principal,
            duration_minutes=max(5, _env_int("AUTH_SESSION_MINUTES", 480)),
        )
        secure = hosted_runtime()
        self.send_json(
            {
                "username": session_principal.username,
                "role": session_principal.role,
                "csrf_token": session_principal.csrf_token,
                "session_expires_at": session_principal.session_expires_at,
            },
            extra_cookies=[
                build_session_cookie(session_token, secure=secure),
                build_csrf_cookie(str(session_principal.csrf_token or ""), secure=secure),
            ],
        )

    def handle_logout(self) -> None:
        if not self.valid_session_csrf():
            self.send_json({"error": "csrf_validation_failed"}, status_code=403)
            return
        token = session_token_from_cookie(self.headers.get("Cookie"))
        store = self.auth_store
        if token and store:
            store.revoke_session(token)
        secure = hosted_runtime()
        self.send_json(
            {"status": "logged_out"},
            extra_cookies=[clear_session_cookie(secure=secure), clear_csrf_cookie(secure=secure)],
        )

    def handle_operator_message(self) -> None:
        inbox = self.operator_inbox
        if inbox is None:
            self.send_json({"error": "operator_inbox_unavailable"}, status_code=503)
            return
        try:
            content_length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            content_length = 0
        if content_length <= 0 or content_length > 110_000:
            self.send_json({"error": "invalid_operator_message_payload"}, status_code=400)
            return
        try:
            payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
            message = inbox.add(
                title=str(payload.get("title") or ""),
                body=str(payload.get("body") or ""),
                created_by=self.principal.username,
                priority=str(payload.get("priority") or "normal"),
                target=str(payload.get("target") or "codex"),
                source="dashboard",
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            self.send_json({"error": str(exc)}, status_code=400)
            return
        self.send_json(
            {
                "message": message,
                "execution_allowed": False,
                "next_action": "manual_agent_review",
            },
            status_code=201,
        )

    def handle_source_refresh(self) -> None:
        try:
            content_length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            content_length = 0
        if content_length <= 0 or content_length > 8192:
            self.send_json({"error": "invalid_source_refresh_payload"}, status_code=400)
            return
        try:
            payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
            sources = payload.get("sources") or (
                "kalshi_current",
                "sports_current",
                "polymarket",
                "kalshi_reference",
            )
            if not isinstance(sources, list | tuple):
                raise ValueError("refresh_sources_must_be_an_array")
            scope = payload.get("scope") or {}
            if not isinstance(scope, dict):
                raise ValueError("refresh_scope_must_be_an_object")
            supplied_key = str(self.headers.get("Idempotency-Key") or "").strip()
            minute_bucket = int(time.time() // 60)
            fallback_key = (
                f"manual:{self.principal.username}:{minute_bucket}:"
                + ",".join(sorted(str(source) for source in sources))
            )
            refresh = SourceDataStore().enqueue_refresh(
                sources=tuple(str(source) for source in sources),
                reason="manual",
                requested_by=self.principal.username,
                scope=scope,
                idempotency_key=supplied_key or fallback_key,
                priority=25,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            self.send_json({"error": str(exc)}, status_code=400)
            return
        except Exception as exc:
            self.send_json(
                {"error": f"source_refresh_queue_unavailable:{type(exc).__name__}"},
                status_code=503,
            )
            return
        self.send_json(
            {
                "request_id": refresh.get("request_id"),
                "status": refresh.get("status"),
                "sources": refresh.get("sources"),
                "status_href": f"/api/v1/source-data/refresh/{refresh.get('request_id')}",
            },
            status_code=202,
        )

    def end_headers(self) -> None:
        for name, value in dashboard_security_headers().items():
            self.send_header(name, value)
        super().end_headers()

    def send_json(
        self,
        payload: dict,
        status_code: int = 200,
        extra_headers: Mapping[str, str] | None = None,
        extra_cookies: list[str] | None = None,
    ) -> None:
        body = json.dumps(payload, indent=2, default=json_default).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        # A mapping cannot carry the session and CSRF cookies at once, so they
        # are emitted as their own repeated Set-Cookie headers.
        for cookie in extra_cookies or []:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_text(self, payload: str, status_code: int = 200, filename: str | None = None) -> None:
        body = payload.encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def client_accepts_gzip(self) -> bool:
        """Whether this request asked for gzip, honouring an explicit refusal.

        `gzip;q=0` is a client saying it does *not* want gzip, so matching the
        bare token would send it an encoding it has declined. A named encoding
        also outranks `*`: RFC 9110 resolves `gzip;q=0, *` to a refusal of gzip
        even though the wildcard would otherwise allow it, so the two are
        collected separately and the specific one wins.
        """
        gzip_quality: float | None = None
        wildcard_quality: float | None = None
        for part in (self.headers.get("Accept-Encoding") or "").split(","):
            token, _, parameters = part.strip().partition(";")
            token = token.strip().lower()
            if token not in {"gzip", "*"}:
                continue
            quality = 1.0
            for parameter in parameters.split(";"):
                key, _, value = parameter.partition("=")
                if key.strip().lower() == "q":
                    try:
                        quality = float(value)
                    except ValueError:
                        quality = 0.0
                    break
            if not 0.0 <= quality <= 1.0:
                quality = 0.0
            if token == "gzip":
                gzip_quality = quality
            else:
                wildcard_quality = quality
        effective = gzip_quality if gzip_quality is not None else wildcard_quality
        return effective is not None and effective > 0

    def send_asset(self, path: str) -> None:
        """Serve a fingerprinted static asset.

        The URL changes whenever the bytes do, so the response can be cached
        for a year while the HTML that references it stays uncached.
        """
        asset = lookup_asset(path)
        if asset is None:
            self.send_error(404)
            return
        body = asset.body
        # Asked before `gzipped` is read: that property compresses on first use,
        # and a client that did not want gzip should not pay for it.
        packed = asset.gzipped if self.client_accepts_gzip() else None
        encoded = packed is not None
        if encoded:
            body = packed
        self.send_response(200)
        self.send_header("Content-Type", asset.content_type)
        self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        # The same URL can now answer with two different encodings, so any
        # shared cache has to key on what the client asked for.
        self.send_header("Vary", "Accept-Encoding")
        if encoded:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_html(
        self,
        payload: str,
        status_code: int = 200,
        extra_cookies: list[str] | None = None,
    ) -> None:
        body = payload.encode("utf-8")
        encoded = False
        if len(body) >= MIN_COMPRESSIBLE_BYTES and self.client_accepts_gzip():
            packed = gzip.compress(body, COMPRESSION_LEVEL)
            if len(packed) < len(body):
                body, encoded = packed, True
        self.send_response(status_code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Vary", "Accept-Encoding")
        if encoded:
            self.send_header("Content-Encoding", "gzip")
        # A mapping cannot carry repeated Set-Cookie headers, so they arrive as
        # a list and are emitted one at a time.
        for cookie in extra_cookies or []:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    @classmethod
    def run_refresh(cls, reason: str, async_run: bool) -> dict:
        if not cls.refresh_config:
            status = {
                "state": "error",
                "accepted": False,
                "message": "Refresh config is not ready.",
            }
            cls.refresh_status = status
            return dict(status)
        now = time.time()
        if reason == "manual" and now - cls.last_manual_refresh_at < REFRESH_COOLDOWN_SECONDS:
            wait_seconds = int(REFRESH_COOLDOWN_SECONDS - (now - cls.last_manual_refresh_at))
            return {
                "state": "rate_limited",
                "accepted": False,
                "status_code": 429,
                "message": f"Refresh cooldown active. Try again in {wait_seconds}s.",
                "wait_seconds": wait_seconds,
            }
        if not cls.refresh_lock.acquire(blocking=False):
            status = dict(cls.refresh_status)
            status["accepted"] = False
            status["status_code"] = 409
            status["message"] = "A refresh is already running. Wait for it to finish."
            return status

        started_at = datetime.now().astimezone().isoformat(timespec="seconds")
        if reason == "manual":
            cls.last_manual_refresh_at = now
        cls.refresh_status = {
            "state": "running",
            "accepted": True,
            "reason": reason,
            "started_at": started_at,
            "message": "Refreshing odds, schedules, public inputs, and slip math.",
        }

        def job() -> None:
            try:
                cleanup_result = cleanup_runtime_storage()
                result = refresh_payload(**cls.refresh_config)
                result["runtime_cleanup"] = cleanup_result
                internal_error = str(result.pop("_internal_error", ""))
                finished_at = datetime.now().astimezone().isoformat(timespec="seconds")
                state = "complete" if result.get("ok") else "error"
                cls.refresh_status = {
                    **result,
                    "state": state,
                    "accepted": True,
                    "reason": reason,
                    "started_at": started_at,
                    "finished_at": finished_at,
                }
                audit_event = {
                    "event": "refresh",
                    "ok": bool(result.get("ok")),
                    "reason": reason,
                    "started_at": started_at,
                    "finished_at": finished_at,
                    "date": result.get("date"),
                    "primary_leg_count": result.get("primary_leg_count", 0),
                    "leverage_leg_count": result.get("leverage_leg_count", 0),
                    "all_day_leg_count": result.get("all_day_leg_count", 0),
                    "research_edge_leg_count": result.get("research_edge_leg_count", 0),
                    "ledger_logged_predictions": result.get("ledger_logged_predictions", 0),
                    "ledger_rejected_predictions": result.get("ledger_rejected_predictions", 0),
                    "ledger_duplicate_rows_ignored": result.get("ledger_duplicate_rows_ignored", 0),
                    "ledger_error": result.get("ledger_error", ""),
                    "runtime_cleanup": cleanup_result,
                    "error": internal_error or result.get("error", ""),
                }
                append_jsonl(cls.audit_path, audit_event)
                if not result.get("ok"):
                    append_jsonl(cls.error_path, audit_event)
            finally:
                cls.refresh_lock.release()

        if async_run:
            thread = threading.Thread(target=job, name=f"paper-refresh-{reason}", daemon=True)
            thread.start()
            return dict(cls.refresh_status)

        job()
        return dict(cls.refresh_status)

    def log_message(self, format: str, *args: object) -> None:
        return


def refresh_payload(
    data_path: Path,
    yyyymmdd: str | None,
    target_probability: float,
    min_leg_probability: float | None,
    max_leg_probability: float,
    min_legs: int,
    max_legs: int,
    stake_dollars: float,
    leverage_min_leg_probability: float,
    public_intel_path: str | Path | None,
) -> dict:
    from .kalshi_ingestion import persist_kalshi_snapshot
    from .today import write_today_payload

    try:
        payload = write_today_payload(
            data_path,
            yyyymmdd,
            slip_target_probability=target_probability,
            slip_min_leg_probability=min_leg_probability,
            slip_max_leg_probability=max_leg_probability,
            slip_min_legs=min_legs,
            slip_max_legs=max_legs,
            slip_stake_dollars=stake_dollars,
            leverage_min_leg_probability=leverage_min_leg_probability,
            public_intel_path=public_intel_path,
        )
        slip = payload.get("custom_slip", {})
        leverage_slip = payload.get("leverage_slip", {})
        all_day_slip = payload.get("all_day_slip", {})
        research_edge_slip = payload.get("research_edge_slip", {})
        print(
            f"Refreshed {data_path} at {payload.get('generated_at')} "
            f"with {slip.get('leg_count', 0)} slip legs."
        )
        try:
            source_persistence = persist_kalshi_snapshot(
                payload,
                worker_name="paper-dashboard-refresh",
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            print(f"Source snapshot persistence failed: {error}")
            return {
                "ok": False,
                "message": "Fresh Kalshi data was collected but could not be committed to PostgreSQL.",
                "error": "postgres_snapshot_persistence_failed",
                "_internal_error": error,
                "generated_at": payload.get("generated_at"),
            }
        try:
            ledger = log_refresh_predictions(payload)
        except Exception as exc:
            ledger = {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
            print(f"Refresh ledger logging failed: {ledger['error']}")
        return {
            "ok": True,
            "message": "Slip refreshed from live data. Reloading dashboard.",
            "generated_at": payload.get("generated_at"),
            "date": payload.get("date"),
            "games": len(payload.get("games", [])),
            "markets": len(payload.get("markets", [])),
            "all_day_market_count": payload.get("all_day_market_count", 0),
            "primary_leg_count": slip.get("leg_count", 0),
            "leverage_leg_count": leverage_slip.get("leg_count", 0),
            "all_day_leg_count": all_day_slip.get("leg_count", 0),
            "research_edge_leg_count": research_edge_slip.get("leg_count", 0),
            "source_persistence_ok": source_persistence.get("ok", True),
            "source_persistence_batch_id": source_persistence.get("batch_id"),
            "source_records_received": source_persistence.get("records_received", 0),
            "source_records_accepted": source_persistence.get("records_accepted", 0),
            "source_records_rejected": source_persistence.get("records_rejected", 0),
            "source_records_duplicated": source_persistence.get("records_duplicated", 0),
            "source_persistence_error": source_persistence.get("error", ""),
            "ledger_ok": bool(ledger.get("ok")),
            "ledger_run_id": ledger.get("run_id"),
            "ledger_run_created": ledger.get("run_created", False),
            "ledger_attempted_predictions": ledger.get("attempted_predictions", 0),
            "ledger_logged_predictions": ledger.get("logged_predictions", 0),
            "ledger_rejected_predictions": ledger.get("rejected_predictions", 0),
            "ledger_duplicate_rows_ignored": ledger.get("duplicate_rows_ignored", 0),
            "ledger_rejection_reasons": ledger.get("rejection_reasons", []),
            "ledger_error": ledger.get("error", ""),
        }
    except Exception as exc:
        payload = load_payload(data_path)
        error = f"{type(exc).__name__}: {exc}"
        payload["refresh_error"] = "live_refresh_failed"
        payload["refresh_failed_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        write_json_atomic(data_path, payload)
        print(f"Refresh failed: {error}")
        return {
            "ok": False,
            "message": "Live refresh failed. Slips are hidden until fresh data returns.",
            "error": "live_refresh_failed",
            "_internal_error": error,
            "failed_at": payload["refresh_failed_at"],
        }


def start_refresh_thread(refresh_seconds: int) -> None:
    def loop() -> None:
        while True:
            time.sleep(refresh_seconds)
            PaperHandler.run_refresh(reason="auto", async_run=False)

    thread = threading.Thread(target=loop, name="paper-refresh", daemon=True)
    thread.start()


def run_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    data_path: str | Path | None = None,
    refresh_seconds: int = 600,
    yyyymmdd: str | None = None,
    target_probability: float = 0.80,
    min_leg_probability: float | None = None,
    max_leg_probability: float = 0.985,
    min_legs: int = 8,
    max_legs: int = 20,
    stake_dollars: float = 5.0,
    leverage_min_leg_probability: float = 0.75,
    public_intel_path: str | Path | None = None,
) -> None:
    resolved_data_path = Path(data_path) if data_path else repo_path("data", "today_paper_view.json")
    PaperHandler.data_path = resolved_data_path
    PaperHandler.refresh_seconds = max(0, int(refresh_seconds or 0))
    PaperHandler.refresh_config = {
        "data_path": resolved_data_path,
        "yyyymmdd": yyyymmdd,
        "target_probability": target_probability,
        "min_leg_probability": min_leg_probability,
        "max_leg_probability": max_leg_probability,
        "min_legs": min_legs,
        "max_legs": max_legs,
        "stake_dollars": stake_dollars,
        "leverage_min_leg_probability": leverage_min_leg_probability,
        "public_intel_path": public_intel_path,
    }
    server = ThreadingHTTPServer((host, port), PaperHandler)
    if hosted_runtime() and not os.environ.get("DASHBOARD_AUTH_PASSWORD"):
        print("Dashboard locked: set DASHBOARD_AUTH_PASSWORD in the hosted environment.")
    if PaperHandler.refresh_seconds:
        PaperHandler.run_refresh(reason="startup", async_run=True)
        start_refresh_thread(PaperHandler.refresh_seconds)
    print(f"Paper view running at http://{host}:{port}")
    print("Health endpoints: /healthz and /readyz")
    if PaperHandler.refresh_seconds:
        print(f"Auto-refreshing every {PaperHandler.refresh_seconds} seconds.")
    server.serve_forever()
