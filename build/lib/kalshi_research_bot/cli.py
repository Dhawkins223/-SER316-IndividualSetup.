from __future__ import annotations

import argparse
import json
import urllib.error
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from .agents import ComboBot, ReportBot, ScrapeBot
from .auth import LocalAuthStore
from .bot_company import bot_company_summary, render_bot_company
from .config import load_json, repo_path
from .connectors.airtable_status import bot_run_payload, sync_status
from .connectors.google_drive_archive import archive_files, default_report_paths
from .connectors.slack_alerts import build_alert_payload, send_alert
from .connectors.status import build_connectors_status, render_connectors_status
from .contracts import Game, MarketQuote, TotalLeg
from .crypto_research import (
    build_crypto_report,
    build_crypto_stage3b_audit_report,
    build_crypto_stage4_diagnostic_report,
    collect_crypto_payload,
    crypto_cycle,
    default_crypto_all_report_path,
    default_crypto_daily_report_path,
    default_crypto_features_path,
    default_crypto_labels_path,
    default_crypto_payload_path,
    default_crypto_stage3b_audit_path,
    default_crypto_stage4_diagnostic_path,
    export_crypto_features,
    log_crypto_predictions,
    render_crypto_report,
    render_crypto_stage3b_audit_report,
    render_crypto_stage4_diagnostic_report,
    settle_crypto_predictions,
    write_crypto_payload,
    write_crypto_report,
    write_crypto_stage3b_audit_report,
    write_crypto_stage4_diagnostic_report,
)
from .daemon import build_daemon_status, render_daemon_status
from .database import database_startup_status, json_default
from .db_migrations import apply_postgres_migrations
from .evaluation.backtest import load_backtest_payload, render_backtest_report, run_backtest, write_backtest_report
from .evaluation.paper_live import (
    build_daily_report,
    build_stage3b_audit_report,
    default_daily_report_path,
    default_run_lock_path,
    default_stage3b_audit_path,
    fetch_official_kalshi_settlements,
    import_settlements,
    load_json_payload,
    log_forward_predictions,
    render_daily_report,
    render_stage3b_audit_report,
    start_paper_test_run,
    write_daily_report,
    write_stage3b_audit_report,
)
from .evaluation.kalshi_decomposition import (
    build_kalshi_return_decomposition,
    default_kalshi_return_decomposition_path,
    render_kalshi_return_decomposition,
    write_kalshi_return_decomposition,
)
from .evaluation.model_audit import (
    build_platform_model_audit,
    default_platform_model_audit_path,
    render_platform_model_audit,
    write_platform_model_audit,
)
from .pipeline import ResearchPipeline
from .retention import (
    RetentionWindowTooShort,
    prune_source_payload_bodies,
    render_storage_report,
    source_payload_storage_report,
)
from .sports_clv import build_sports_clv_report, capture_sports_closing_lines, render_sports_clv_report
from .paper_server import run_server
from .sports_research import (
    append_sports_validation_ledger,
    build_sports_report,
    collect_sports_payload,
    default_sports_all_report_path,
    default_sports_daily_report_path,
    default_sports_features_path,
    default_sports_labels_path,
    default_sports_payload_path,
    default_sports_validation_ledger_path,
    export_sports_features,
    log_sports_predictions,
    read_sports_validation_ledger,
    render_sports_report,
    settle_sports_predictions,
    sports_cycle,
    write_sports_payload,
    write_sports_report,
)
from .source_quality import (
    build_data_quality_report,
    default_data_quality_json_path,
    default_data_quality_report_path,
    render_data_quality_report,
    write_data_quality_report,
)
from .business_store import create_store
from .today import write_today_payload
from .monitoring import build_internal_status
from .operator_inbox import OperatorInbox, PRIORITIES, STATUSES, TARGETS
from .worker_runtime import run_worker_forever, run_worker_once, start_worker_health_server
from .worker_services import SERVICE_SPECS, build_service_operation, service_run_id


def load_games(path: str | Path) -> list[Game]:
    payload = load_json(path)
    return [Game(**game) for game in payload.get("games", [])]


def load_quotes(path: str | Path) -> list[MarketQuote]:
    payload = load_json(path)
    return [MarketQuote(**quote) for quote in payload.get("quotes", [])]


def load_total_legs(path: str | Path) -> list[TotalLeg]:
    payload = load_json(path)
    return [TotalLeg(**leg) for leg in payload.get("legs", [])]


def run_demo(args: argparse.Namespace) -> int:
    games = load_games(repo_path("examples", "sample_games.json"))
    quotes = load_quotes(repo_path("examples", "sample_quotes.json"))
    edges = ResearchPipeline().run(games, quotes, min_edge_cents=args.min_edge)
    if args.save_db:
        create_store().insert_edge_results(edges)
    print(ReportBot().render_edges(edges))
    return 0


def run_research(args: argparse.Namespace) -> int:
    games = load_games(args.games)
    quotes = load_quotes(args.quotes)
    edges = ResearchPipeline().run(games, quotes, min_edge_cents=args.min_edge)
    if args.save_db:
        create_store().insert_edge_results(edges)
    print(ReportBot().render_edges(edges))
    return 0


def run_collect(args: argparse.Namespace) -> int:
    records = ScrapeBot().collect(args.sources)
    if args.save_db:
        create_store().insert_source_records(records)
    for record in records:
        print(f"[{record.kind}] {record.source}: {record.title}")
        print(record.url)
        if record.metadata:
            print(record.metadata)
        print()
    return 0


def run_combo(args: argparse.Namespace) -> int:
    legs = load_total_legs(args.legs)
    combos = ComboBot().build_ranked_combos(
        legs,
        target_probability=args.target,
        min_legs=args.min_legs,
        max_legs=args.max_legs,
        max_results=args.max_results,
        min_leg_probability=args.min_leg_probability,
    )
    print(ReportBot().render_combos(combos))
    return 0


def _blocked_public_source(exc: BaseException) -> int:
    """Report an unreachable public source the way the collector already does.

    ``fetch_espn_schedule_with_status`` catches exactly these and records the
    endpoint as blocked or failed with a reason. The Kalshi fetch does not, so a
    403 or an outage took the whole payload build down with a stack trace --
    which tells an operator nothing they can act on, and looks like a bug in the
    tool rather than a source being unavailable.

    Nothing is degraded quietly: no payload is written and the exit status is
    non-zero, exactly as before. Only the reporting changes.
    """

    reason = getattr(exc, "reason", None) or exc
    print(f"Public source unavailable: {type(exc).__name__}: {reason}")
    print("No payload was written. Re-run once the source is reachable.")
    return 1


PUBLIC_SOURCE_ERRORS = (TimeoutError, urllib.error.URLError)


def run_today(args: argparse.Namespace) -> int:
    try:
        payload = write_today_payload(args.output, args.date, public_intel_path=args.public_intel)
    except PUBLIC_SOURCE_ERRORS as exc:
        return _blocked_public_source(exc)
    print(f"Wrote {args.output}")
    print(f"Games: {len(payload.get('games', []))}")
    print(f"Kalshi combo markets: {len(payload.get('markets', []))}")
    pick = payload.get("pick_summary", {})
    print(f"Bot action: {pick.get('action', 'UNKNOWN')}")
    if pick.get("candidates"):
        best = pick["candidates"][0]
        print(f"Top research candidate: {best['ticker']} @ {float(best['yes_ask_cents']):.2f}c")
        print(f"Model probability: {float(best['model_probability']):.2%}")
        print(f"Lower-bound net edge: {float(best['lower_bound_net_edge_cents']):.2f}c")
        print("Execution: disabled (research only)")
    return 0


def run_paper(args: argparse.Namespace) -> int:
    run_server(
        args.host,
        args.port,
        data_path=args.output,
        refresh_seconds=args.refresh_seconds,
        yyyymmdd=args.date,
        target_probability=args.target,
        min_leg_probability=args.min_leg_probability,
        max_leg_probability=args.max_leg_probability,
        min_legs=args.min_legs,
        max_legs=args.max_legs,
        stake_dollars=args.stake,
        leverage_min_leg_probability=args.leverage_target,
        public_intel_path=args.public_intel,
    )
    return 0


def run_database_status(args: argparse.Namespace) -> int:
    print(json.dumps(database_startup_status(), indent=2, sort_keys=True, default=json_default))
    return 0


def run_preflight(args: argparse.Namespace) -> int:
    """Report every deployment gate at once, and exit non-zero if one blocks.

    Read-only by design: an operator has to be able to point this at production
    without weighing whether it will change something.
    """
    from .preflight import EXIT_FAILED, EXIT_OK, render_preflight, run_preflight as evaluate

    report = evaluate()
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=json_default))
    else:
        print(render_preflight(report))
    return EXIT_OK if report["ready"] else EXIT_FAILED


def run_database_migrate(args: argparse.Namespace) -> int:
    from .database import DatabaseSettings

    settings = DatabaseSettings.from_env()
    if not settings.database_url:
        print("PostgreSQL migration blocked: DATABASE_URL is missing.")
        return 2
    result = apply_postgres_migrations(
        settings.require_url(),
        statement_timeout_ms=settings.migration_statement_timeout_ms,
    )
    print(json.dumps(result, indent=2, sort_keys=True, default=json_default))
    return 0


def run_auth_create_user(args: argparse.Namespace) -> int:
    if str(os.environ.get("AUTH_REGISTRATION_ENABLED", "false")).lower() not in {"1", "true", "yes", "on"}:
        print("Account creation blocked: set AUTH_REGISTRATION_ENABLED=true for this local command.")
        return 2
    password = os.environ.get("AUTH_NEW_USER_PASSWORD")
    if not password:
        print("Account creation blocked: AUTH_NEW_USER_PASSWORD is missing.")
        return 2
    created = LocalAuthStore().create_user(args.username, password, role=args.role)
    print(json.dumps(created, indent=2, sort_keys=True, default=json_default))
    return 0


def run_auth_disable_user(args: argparse.Namespace) -> int:
    changed = LocalAuthStore().set_disabled(args.username, disabled=not args.enable)
    print(json.dumps({"username": args.username, "disabled": not args.enable, "updated": changed}, indent=2, default=json_default))
    return 0 if changed else 1


def run_operator_message_add(args: argparse.Namespace) -> int:
    body_path = Path(args.file)
    if not body_path.is_file():
        print(f"Operator message blocked: file not found: {body_path}")
        return 2
    try:
        message = OperatorInbox().add(
            title=args.title,
            body=body_path.read_text(encoding="utf-8"),
            created_by=args.created_by,
            priority=args.priority,
            target=args.target,
            source="cli",
            message_id=args.message_id,
        )
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        print(f"Operator message blocked: {exc}")
        return 2
    print(json.dumps(message, indent=2, sort_keys=True, default=json_default))
    print("Queued for manual agent review; no command or code was executed.")
    return 0


def run_operator_message_list(args: argparse.Namespace) -> int:
    inbox = OperatorInbox()
    messages = inbox.list(
        status=args.status,
        target=args.target,
        limit=args.limit,
    )
    print(json.dumps({"counts": inbox.counts(), "messages": messages}, indent=2, sort_keys=True, default=json_default))
    return 0


def run_operator_message_claim(args: argparse.Namespace) -> int:
    try:
        message = OperatorInbox().claim(args.message_id, agent=args.agent)
    except ValueError as exc:
        print(f"Operator message claim blocked: {exc}")
        return 2
    print(json.dumps(message, indent=2, sort_keys=True, default=json_default))
    return 0


def run_operator_message_complete(args: argparse.Namespace) -> int:
    summary_path = Path(args.summary_file)
    if not summary_path.is_file():
        print(f"Operator message completion blocked: file not found: {summary_path}")
        return 2
    try:
        message = OperatorInbox().complete(
            args.message_id,
            agent=args.agent,
            summary=summary_path.read_text(encoding="utf-8"),
        )
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        print(f"Operator message completion blocked: {exc}")
        return 2
    print(json.dumps(message, indent=2, sort_keys=True, default=json_default))
    return 0


def run_worker_command(args: argparse.Namespace) -> int:
    spec = SERVICE_SPECS[args.service]
    operation = build_service_operation(
        args.service,
        kalshi_run_id=args.kalshi_run_id,
        crypto_run_id=args.crypto_run_id,
        sports_run_id=args.sports_run_id,
    )
    run_id = service_run_id(
        args.service,
        kalshi_run_id=args.kalshi_run_id,
        crypto_run_id=args.crypto_run_id,
        sports_run_id=args.sports_run_id,
    )
    if args.once:
        result = run_worker_once(
            spec,
            operation,
            run_id=run_id,
            idempotency_key=args.idempotency_key,
        )
        return 0 if result["status"] in {"success", "skipped_duplicate"} else 1
    return run_worker_forever(spec, operation, run_id=run_id)


def run_worker_status(args: argparse.Namespace) -> int:
    print(json.dumps(build_internal_status(), indent=2, sort_keys=True, default=json_default))
    return 0


HOSTED_WEB_REFRESH_SECONDS = 300


def hosted_web_refresh_seconds() -> int:
    """How often the hosted dashboard refreshes its own paper view.

    This entry point used to hardcode zero, which disables the startup refresh,
    the background thread, and the page's meta-refresh. That made `service-start`
    a silent downgrade from the start command production actually runs, and
    `/readyz` reports `fresh_data_ready` only while those refreshes keep
    happening. The cadence is now read from the environment and defaults to what
    the hosted dashboard already uses, so the repository's declared start command
    and the deployed one mean the same thing. Zero is still accepted, for a
    dashboard that reads only what the collector workers write.
    """

    raw = os.environ.get("DASHBOARD_REFRESH_SECONDS")
    if raw is None or not str(raw).strip():
        return HOSTED_WEB_REFRESH_SECONDS
    try:
        return max(0, int(str(raw).strip()))
    except (TypeError, ValueError):
        return HOSTED_WEB_REFRESH_SECONDS


def run_hosted_service(args: argparse.Namespace) -> int:
    service = str(os.environ.get("HAWKNETIC_SERVICE") or "web").strip()
    if service == "web":
        run_server(
            host="0.0.0.0",
            port=int(os.environ.get("PORT") or "8765"),
            refresh_seconds=hosted_web_refresh_seconds(),
        )
        return 0
    if service not in SERVICE_SPECS:
        print(f"Hosted service blocked: unknown HAWKNETIC_SERVICE={service!r}")
        return 2
    worker_args = argparse.Namespace(
        service=service,
        once=False,
        idempotency_key=None,
        kalshi_run_id=os.environ.get("KALSHI_RUN_ID", "stage3a_20260703_170707"),
        crypto_run_id=os.environ.get("CRYPTO_RUN_ID", "crypto_private_20260704"),
        sports_run_id=os.environ.get("SPORTS_RUN_ID", "sports_private_20260704"),
    )
    health_server = start_worker_health_server(service, int(os.environ.get("PORT") or "8765"))
    try:
        return run_worker_command(worker_args)
    finally:
        health_server.shutdown()
        health_server.server_close()


def run_pick(args: argparse.Namespace) -> int:
    try:
        payload = write_today_payload(args.output, args.date)
    except PUBLIC_SOURCE_ERRORS as exc:
        return _blocked_public_source(exc)
    pick = payload.get("pick_summary", {})
    print(f"Bot action: {pick.get('action', 'UNKNOWN')}")
    print(pick.get("reason", ""))
    print(f"Tradable combos scanned: {pick.get('tradable_combo_count', 0)}")
    candidates = pick.get("candidates", [])
    if not candidates:
        print("No validated research candidate generated.")
        return 0
    best = candidates[0]
    print()
    print("RESEARCH CANDIDATE")
    print(f"Ticker: {best['ticker']}")
    print(f"YES ask: {float(best['yes_ask_cents']):.2f}c")
    print(f"Model probability: {float(best['model_probability']):.2%}")
    print(f"Lower-bound net edge: {float(best['lower_bound_net_edge_cents']):.2f}c")
    print("Execution: disabled (research only)")
    print("Legs:")
    for leg in best.get("legs", []):
        probability = leg.get("market_implied_probability")
        probability_text = "n/a" if probability is None else f"{probability:.2%}"
        print(f"- {leg.get('side', '').upper()} {leg.get('subtitle') or leg.get('title')} ({probability_text})")
    return 0


def run_slip(args: argparse.Namespace) -> int:
    try:
        payload = write_today_payload(
            args.output,
            args.date,
            slip_target_probability=args.target,
            slip_min_leg_probability=args.min_leg_probability,
            slip_max_leg_probability=args.max_leg_probability,
            slip_min_legs=args.min_legs,
            slip_max_legs=args.max_legs,
            slip_stake_dollars=args.stake,
            public_intel_path=args.public_intel,
        )
    except PUBLIC_SOURCE_ERRORS as exc:
        return _blocked_public_source(exc)
    slip = payload.get("custom_slip", {})
    print(f"Bot action: {slip.get('action', 'UNKNOWN')}")
    if slip.get("action") != "BUILD_SLIP":
        print(slip.get("reason", "No slip generated."))
        print(f"Eligible legs: {slip.get('eligible_leg_count', 0)}")
        return 0
    print(f"Min individual leg probability: {slip['min_leg_probability']:.0%}")
    print(f"Full slip chance: {slip['adjusted_probability']:.2%}")
    print(f"Raw multiplied chance: {slip['raw_probability']:.2%}")
    print(f"Legs: {slip['leg_count']}")
    print(f"Sports: {', '.join(slip['sports'])}")
    print(f"Estimated combo price: {slip['estimated_combo_price_cents']:.2f}c")
    print(f"${slip['stake_dollars']:.2f} estimated payout if right: ${slip['estimated_payout_if_right']:.2f}")
    print()
    print("SLIP")
    current_sport = None
    for leg in slip.get("legs", []):
        if leg["sport"] != current_sport:
            current_sport = leg["sport"]
            print(current_sport)
        label = leg.get("subtitle") or leg.get("title") or leg.get("market_ticker")
        event = leg.get("display_event") or leg.get("event_ticker", "")
        print(f"- {event}: {leg.get('side', '').upper()} {label} ({leg['probability']:.2%})")
    return 0


def run_backtest_command(args: argparse.Namespace) -> int:
    payload = load_backtest_payload(args.input)
    report = run_backtest(payload)
    if args.output:
        write_backtest_report(report, args.output)
        print(f"Wrote {args.output}")
    print(render_backtest_report(report))
    return 0


def run_paper_run_start(args: argparse.Namespace) -> int:
    store = create_store()
    run = start_paper_test_run(
        store,
        run_id=args.run_id,
        lock_path=args.lock_path,
    )
    print(f"Started private paper run: {run['run_id']}")
    print(f"Config hash: {run['config_hash']}")
    print(f"Lock file: {args.lock_path}")
    return 0


def run_paper_log(args: argparse.Namespace) -> int:
    store = create_store()
    payload = load_json_payload(args.input)
    result = log_forward_predictions(store, payload, run_id=args.run_id)
    print(f"Run: {args.run_id}")
    print(f"Attempted predictions: {result['attempted_predictions']}")
    print(f"Logged predictions: {result['logged_predictions']}")
    print(f"Rejected predictions: {result['rejected_predictions']}")
    print(f"Duplicate rows ignored: {result.get('duplicate_rows_ignored', 0)}")
    if result["rejection_reasons"]:
        print(f"Rejection reasons: {', '.join(result['rejection_reasons'])}")
    if args.report:
        report = build_daily_report(store, run_id=args.run_id, date=args.date)
        write_daily_report(report, args.report)
        print(f"Wrote report: {args.report}")
    return 0


def run_paper_settle(args: argparse.Namespace) -> int:
    store = create_store()
    settlements = load_json_payload(args.settlements)
    result = import_settlements(store, run_id=args.run_id, settlements_payload=settlements)
    print(f"Run: {args.run_id}")
    print(f"Settlement markets available: {result['settlements_available']}")
    print(f"Prediction rows updated: {result['rows_updated']}")
    if args.report:
        report = build_daily_report(store, run_id=args.run_id, date=args.date)
        write_daily_report(report, args.report)
        print(f"Wrote report: {args.report}")
    return 0


def run_paper_settle_kalshi(args: argparse.Namespace) -> int:
    store = create_store()
    settlements = fetch_official_kalshi_settlements(store, run_id=args.run_id)
    result = import_settlements(store, run_id=args.run_id, settlements_payload=settlements)
    print(f"Run: {args.run_id}")
    print(f"Settlement source: {result['settlement_source']}")
    print(f"Settlement markets available: {result['settlements_available']}")
    print(f"Prediction rows updated: {result['rows_updated']}")
    print(f"Settlement issue rows updated: {result['issue_rows_updated']}")
    if result.get("settlement_issue_counts"):
        print(f"Settlement issues: {result['settlement_issue_counts']}")
    if result.get("fetch_errors"):
        print(f"Settlement fetch errors: {len(result['fetch_errors'])}")
    if args.report:
        report = build_daily_report(store, run_id=args.run_id, date=args.date)
        write_daily_report(report, args.report)
        print(f"Wrote report: {args.report}")
    return 0


def run_paper_report(args: argparse.Namespace) -> int:
    store = create_store()
    report = build_daily_report(store, run_id=args.run_id, date=args.date)
    if args.output:
        write_daily_report(report, args.output)
        print(f"Wrote {args.output}")
    print(render_daily_report(report))
    return 0


def run_paper_stage3b_audit(args: argparse.Namespace) -> int:
    store = create_store()
    report = build_stage3b_audit_report(store, run_id=args.run_id)
    if args.output:
        write_stage3b_audit_report(report, args.output)
        print(f"Wrote {args.output}")
    print(render_stage3b_audit_report(report))
    return 0


def run_kalshi_return_audit(args: argparse.Namespace) -> int:
    store = create_store()
    report = build_kalshi_return_decomposition(store, run_id=args.run_id)
    if args.output:
        write_kalshi_return_decomposition(report, args.output)
        print(f"Wrote {args.output}")
        print(f"Wrote {Path(args.output).with_suffix('.json')}")
    print(render_kalshi_return_decomposition(report))
    return 0


def run_model_evaluate(args: argparse.Namespace) -> int:
    report = build_platform_model_audit(
        kalshi_run_id=args.kalshi_run_id,
        crypto_run_id=args.crypto_run_id,
        sports_run_id=args.sports_run_id,
        persist=not args.no_persist,
    )
    if args.output:
        write_platform_model_audit(report, args.output)
        print(f"Wrote {args.output}")
        print(f"Wrote {Path(args.output).with_suffix('.json')}")
    print(render_platform_model_audit(report))
    return 0


def run_crypto_collect(args: argparse.Namespace) -> int:
    payload = collect_crypto_payload()
    write_crypto_payload(args.output, payload)
    print(f"Wrote {args.output}")
    print(f"Crypto records: {len(payload.get('records', []))}")
    if payload.get("errors"):
        print(f"Source errors: {payload['errors']}")
    return 0


def run_crypto_log(args: argparse.Namespace) -> int:
    payload = load_json_payload(args.input)
    result = log_crypto_predictions(run_id=args.run_id, payload=payload)
    print(f"Run: {args.run_id}")
    print(f"Attempted predictions: {result['attempted_predictions']}")
    print(f"Logged predictions: {result['logged_predictions']}")
    print(f"Rejected predictions: {result['rejected_predictions']}")
    print(f"Duplicate rows ignored: {result['duplicate_rows_ignored']}")
    print(f"Rejection reasons: {result['rejection_reasons']}")
    if args.report:
        report = build_crypto_report(run_id=args.run_id)
        write_crypto_report(report, args.report)
        print(f"Wrote report: {args.report}")
    return 0


def run_crypto_settle(args: argparse.Namespace) -> int:
    payload = load_json_payload(args.input)
    result = settle_crypto_predictions(run_id=args.run_id, payload=payload)
    print(f"Run: {args.run_id}")
    print(f"Prediction rows updated: {result['rows_updated']}")
    print(f"Unresolved rows: {result['unresolved_rows']}")
    print(f"Settlement issues: {result['settlement_issue_counts']}")
    if args.report:
        report = build_crypto_report(run_id=args.run_id)
        write_crypto_report(report, args.report)
        print(f"Wrote report: {args.report}")
    return 0


def run_crypto_report(args: argparse.Namespace) -> int:
    report = build_crypto_report(run_id=args.run_id)
    if args.output:
        write_crypto_report(report, args.output)
        print(f"Wrote {args.output}")
    print(render_crypto_report(report))
    return 0


def run_crypto_stage3b_audit(args: argparse.Namespace) -> int:
    report = build_crypto_stage3b_audit_report(run_id=args.run_id)
    if args.output:
        write_crypto_stage3b_audit_report(report, args.output)
        print(f"Wrote {args.output}")
    print(render_crypto_stage3b_audit_report(report))
    return 0


def run_crypto_stage4_diagnostic(args: argparse.Namespace) -> int:
    report = build_crypto_stage4_diagnostic_report(run_id=args.run_id)
    if args.output:
        write_crypto_stage4_diagnostic_report(report, args.output)
        print(f"Wrote {args.output}")
    print(render_crypto_stage4_diagnostic_report(report))
    return 0


def run_crypto_cycle(args: argparse.Namespace) -> int:
    result = crypto_cycle(run_id=args.run_id, output=args.output)
    report = result["report"]
    print(f"Run: {args.run_id}")
    print(f"Payload: {result['payload_path']}")
    print(f"Heartbeat status: {report.get('heartbeat_status', 'unknown')}")
    print(f"Logged predictions: {result['log_result']['logged_predictions']}")
    print(f"Rejected predictions: {result['log_result']['rejected_predictions']}")
    if result["log_result"].get("rejection_reasons"):
        print(f"Rejection reasons: {result['log_result']['rejection_reasons']}")
    print(f"Settled rows: {result['settle_result']['rows_updated']}")
    print(f"Unresolved predictions: {report.get('unresolved_predictions')}")
    print(f"Source error count: {report.get('source_error_count', 0)}")
    if report.get("blockers"):
        print(f"Blockers: {report['blockers']}")
    if report.get("source_errors"):
        reasons = sorted({str(error.get("error") or error.get("reason") or "source_fetch_error") for error in report["source_errors"]})
        print(f"Source error reasons: {reasons}")
    print(f"Gate: {report['gate_result']}")
    return 0


def run_crypto_export_features(args: argparse.Namespace) -> int:
    result = export_crypto_features(run_id=args.run_id, output=args.output, labels_output=args.labels_output)
    print(f"Feature rows: {result['feature_rows']}")
    print(f"Label rows: {result['label_rows']}")
    print(f"Wrote {result['output']}")
    if result.get("labels_output"):
        print(f"Wrote {result['labels_output']}")
    return 0


def run_sports_collect(args: argparse.Namespace) -> int:
    payload = collect_sports_payload(sport_key=args.sport)
    write_sports_payload(args.output, payload)
    print(f"Wrote {args.output}")
    if payload.get("blocker"):
        print(f"Blocker: {payload['blocker']} ({payload.get('required_env_var')})")
    print(f"Sports records: {len(payload.get('records', []))}")
    return 0


def run_sports_log(args: argparse.Namespace) -> int:
    payload = load_json_payload(args.input)
    result = log_sports_predictions(run_id=args.run_id, payload=payload)
    print(f"Run: {args.run_id}")
    if result.get("blocker"):
        print(f"Blocker: {result['blocker']}")
    print(f"Attempted predictions: {result['attempted_predictions']}")
    print(f"Logged predictions: {result['logged_predictions']}")
    print(f"Rejected predictions: {result['rejected_predictions']}")
    print(f"Duplicate rows ignored: {result['duplicate_rows_ignored']}")
    print(f"Rejection reasons: {result['rejection_reasons']}")
    if args.report:
        report = build_sports_report(run_id=args.run_id)
        write_sports_report(report, args.report)
        print(f"Wrote report: {args.report}")
    return 0


def run_sports_settle(args: argparse.Namespace) -> int:
    finals = load_json_payload(args.finals)
    result = settle_sports_predictions(run_id=args.run_id, finals_payload=finals)
    print(f"Run: {args.run_id}")
    print(f"Prediction rows updated: {result['rows_updated']}")
    print(f"Unresolved rows: {result['unresolved_rows']}")
    print(f"Settlement issues: {result['settlement_issue_counts']}")
    if args.report:
        report = build_sports_report(run_id=args.run_id)
        write_sports_report(report, args.report)
        print(f"Wrote report: {args.report}")
    return 0


def run_raw_retention(args: argparse.Namespace) -> int:
    print(render_storage_report(source_payload_storage_report(older_than_days=args.older_than_days)))
    print("")
    if args.report_only:
        return 0
    try:
        result = prune_source_payload_bodies(
            older_than_days=args.older_than_days,
            source=args.source,
            limit=args.limit,
            dry_run=not args.apply,
        )
    except RetentionWindowTooShort as exc:
        print(f"Refused: {exc}")
        return 2
    mode = "APPLIED" if args.apply else "DRY RUN (pass --apply to write)"
    print(f"{mode}")
    print(f"Cutoff: {result['cutoff']}")
    print(f"Candidates in this pass: {result['candidates']}")
    print(f"Bodies pruned: {result['pruned']}")
    print(f"Reclaimable in this pass: {result['reclaimable_bytes']} bytes")
    print(f"Still eligible after this pass: {result['remaining_after_limit']}")
    print("")
    print(result["note"])
    return 0


def run_sports_clv(args: argparse.Namespace) -> int:
    run_id = args.run_id or None
    if not args.report_only:
        capture = capture_sports_closing_lines(run_id=run_id)
        print(f"Markets closed: {capture['markets_closed']}")
        print(f"Rows updated: {capture['rows_updated']}")
        print(f"Rows already current: {capture['rows_unchanged']}")
        print("")
    report = build_sports_clv_report(run_id=run_id, limit=args.limit)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(report, indent=2, default=json_default), encoding="utf-8")
        print(f"Wrote {args.output}")
    print(render_sports_clv_report(report))
    return 0


def run_sports_ratings(args: argparse.Namespace) -> int:
    """Rate teams from settled games and state how the rating scored."""
    from .sports_ratings import (
        EloConfig,
        build_historical_ratings_report,
        build_sports_ratings_report,
        record_sports_ratings_experiment,
        render_sports_ratings_report,
    )

    since = None
    if args.since:
        since = datetime.fromisoformat(str(args.since).replace("Z", "+00:00"))
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
    elo_config = EloConfig(
        k_factor=Decimal(str(args.k_factor)),
        home_advantage=Decimal(str(args.home_advantage)),
        min_team_games=args.min_team_games,
    )
    if args.historical:
        seasons = None
        if args.seasons:
            seasons = [int(value) for value in str(args.seasons).replace(",", " ").split()]
        report = build_historical_ratings_report(
            config=elo_config,
            min_evaluated_games=args.min_games,
            seasons=seasons,
            regular_season_only=args.regular_season_only,
        )
    else:
        report = build_sports_ratings_report(
            league=args.league,
            since=since,
            config=elo_config,
            min_evaluated_games=args.min_games,
        )
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(report, indent=2, default=json_default), encoding="utf-8")
        print(f"Wrote {args.output}")
    print(render_sports_ratings_report(report))
    if args.record:
        entries = record_sports_ratings_experiment(report)
        print("")
        if not entries:
            print("Not recorded: the run has too few evaluated games to be an experiment.")
            return 0
        for entry in entries:
            print(f"Recorded vs {entry['baseline']}: {entry['decision']} ({entry['entry_hash']})")
    return 0


def run_market_blend(args: argparse.Namespace) -> int:
    """Ask whether anything the model knows improves on the closing price."""
    from .connectors.nflverse import load_nflverse_games
    from .sports_market_model import (
        MarketBlendConfig,
        build_market_blend_report,
        render_market_blend_report,
        required_games_for_blend_effect,
    )
    from .sports_ratings import EloConfig, market_home_probabilities, load_settled_games

    config = MarketBlendConfig(
        min_training_rows=args.min_training_rows,
        elo=EloConfig(min_team_games=args.min_team_games),
    )
    if args.historical:
        seasons = None
        if args.seasons:
            seasons = [int(value) for value in str(args.seasons).replace(",", " ").split()]
        dataset = load_nflverse_games(seasons=seasons, regular_season_only=args.regular_season_only)
        report = build_market_blend_report(
            dataset.games,
            dataset.market_probabilities,
            config=config,
            source="nflverse_historical_archive",
            dataset_version=dataset.dataset_version(),
            league="nfl",
            market_baseline_name="devigged_reported_close",
        )
        report["dataset"] = dataset.evidence()
    else:
        games, _ = load_settled_games(league=args.league)
        report = build_market_blend_report(
            games,
            market_home_probabilities(league=args.league),
            config=config,
            source="collected_settled_games",
            dataset_version=f"collected_settled_games:{args.league or 'all'}:{len(games)}",
            league=args.league,
        )

    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(report, indent=2, default=json_default), encoding="utf-8")
        print(f"Wrote {args.output}")
    print(render_market_blend_report(report))
    required = required_games_for_blend_effect(report)
    if required:
        print(f"  games needed to resolve this effect: {required.get('required_sample'):,}")
    if args.record:
        from .sports_ratings import record_sports_ratings_experiment

        entries = record_sports_ratings_experiment(report)
        print("")
        if not entries:
            print("Not recorded: no comparison produced an interval.")
        for entry in entries:
            print(f"Recorded vs {entry['baseline']}: {entry['decision']} ({entry['entry_hash']})")
    return 0


def run_venue_compare(args: argparse.Namespace) -> int:
    """Compare the sportsbook board against Polymarket on the same games.

    Section O measured that this platform's rating loses to the closing line, so
    the remaining question is not whether a model knows better but whether two
    venues disagree with each other. This answers that for the games both price.
    """
    from .connectors.http import live_probe_client
    from .connectors.polymarket import fetch_polymarket_markets, normalize_polymarket_markets
    from .sports_board import load_sports_board
    from .venue_compare import compare_venues, render_venue_comparison

    board = load_sports_board()
    if board.get("board_state") != "fresh":
        print(
            f"Sports board is '{board.get('board_state')}' ({board.get('state_reason')}); "
            "no comparison is made against a board that is not fresh."
        )
        return 2

    payload, fetched_at, status, not_live = fetch_polymarket_markets(
        client=live_probe_client(), limit=args.limit, order_by="volume24hr"
    )
    if payload is None or not_live:
        print(f"Polymarket unreachable (status {status}{'; ' + not_live if not_live else ''}).")
        return 2

    normalization = normalize_polymarket_markets(
        payload, api_fetched_at=fetched_at, source_url="gamma"
    )
    report = compare_venues(
        board, normalization.markets, start_tolerance_minutes=args.start_tolerance_minutes
    )
    report["polymarket_fetched_at"] = fetched_at
    print(render_venue_comparison(report, limit=args.top))
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(report, indent=2, default=json_default), encoding="utf-8")
        print(f"Wrote {args.output}")
    return 0


def run_slip_analyze(args: argparse.Namespace) -> int:
    """Grade a slip from a JSON file of legs, or demonstrate the correlation effect.

    The file is a list of objects carrying at least ``decimal_odds`` and a
    de-vigged ``no_vig_probability``; ``event_id`` and ``league`` are what make
    the correlation adjustment mean anything, since legs that share nothing are
    independent and the copula reduces to the plain product.
    """
    from .slip_analysis import analyze_slip, legs_from_board, recommend_trim

    rows = json.loads(Path(args.legs).read_text(encoding="utf-8"))
    legs = legs_from_board(rows)
    if not legs:
        print(
            "No usable legs. Every leg needs decimal_odds and a de-vigged "
            "no_vig_probability; rows missing either are skipped rather than "
            "priced with the bookmaker's margin still in them."
        )
        return 2

    report = analyze_slip(legs, stake=args.stake, draws=args.draws)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=json_default))
        return 0

    pct = lambda v: f"{v:.2%}"  # noqa: E731 - local shorthand
    print(f"Slip: {report['leg_count']} legs at {report['combined_decimal_odds']:.2f}")
    print(f"  Stake {args.stake:.2f} -> payout {report['payout_if_won']:.2f}")
    print("")
    print(f"  Break-even needed      {pct(report['break_even_probability'])}")
    print(f"  Independent product    {pct(report['independent_probability'])}")
    lo, hi = report["hit_probability_interval"]
    print(f"  Correlation-adjusted   {pct(report['hit_probability'])}  [{pct(lo)} – {pct(hi)}]")
    if report["precision"] == "insufficient_draws":
        print(f"      ^ only {report['simulation']['hits']} simulated hits — rerun with "
              f"--draws {max(200_000, args.draws * 20):,} before quoting this")
    ratio = report["independence_error_ratio"]
    if ratio is not None:
        print(f"  Independence error     {ratio:.2f}x  ({report['correlated_pairs']} correlated pairs)")
    print("")
    # Percentage points, not percent: this is a difference of two probabilities.
    print(f"  Edge over break-even   {report['edge_over_break_even'] * 100:+.2f} pts")
    if report["expected_value_is_achievable"]:
        print(f"  Expected value         {report['expected_value']:+.2f} ({report['expected_value_ratio']:+.1%})")
    else:
        print(f"  Expected value         {report['expected_value']:+.2f} — NOT ACHIEVABLE at these prices")
    print(f"  Risk tier              {report['risk_tier']}")
    print(f"  Verdict                {report['verdict'].replace('_', ' ').upper()}")

    warning = report.get("same_event_repricing_warning")
    if warning:
        print("")
        print(f"  ! {warning['legs_sharing_an_event']} legs share "
              f"{warning['events_with_multiple_legs']} game(s).")
        print("    A book prices a same-game parlay jointly, not by multiplying")
        print("    standalone legs. The probability above is informative; the")
        print("    expected value beside it is not takeable at these prices.")

    if args.trim and len(legs) > 2:
        trim = recommend_trim(legs, stake=args.stake)
        print("")
        print(f"  Trim to {trim['recommended_leg_count']} legs, dropping: {', '.join(trim['drop']) or 'nothing'}")
        print(f"  Expected value gain    {trim['improvement_in_expected_value_ratio']:+.1%}")

    print("")
    print("  Research only. This is arithmetic on posted prices, not a forecast")
    print("  that beats them, and not a recommendation to place a wager.")
    return 0


def run_power_audit(args: argparse.Namespace) -> int:
    """Ask what the evidence on hand could ever have detected (E-09).

    This runs the same market-blend comparison the research program runs, then
    reports the floor beneath it: the smallest paired improvement that sample
    could have distinguished from zero. A result under that floor is not a weak
    finding, it is an unreadable one, and the seasons-required column is what
    turns "needs more data" into a number somebody has to look at.
    """
    from .connectors.nflverse import load_nflverse_games
    from .sports_market_model import MarketBlendConfig, build_market_blend_report
    from .sports_power_audit import (
        PUBLISHED_SCHEDULES,
        audit_detectability,
        combined_volume,
        quote_inflation,
        record_power_audit_experiment,
        render_power_audit_report,
        volume_for_league,
    )
    from .sports_ratings import EloConfig, load_settled_games, market_home_probabilities

    config = MarketBlendConfig(elo=EloConfig(min_team_games=args.min_team_games))
    if args.historical:
        seasons = None
        if args.seasons:
            seasons = [int(value) for value in str(args.seasons).replace(",", " ").split()]
        dataset = load_nflverse_games(seasons=seasons, regular_season_only=args.regular_season_only)
        report = build_market_blend_report(
            dataset.games,
            dataset.market_probabilities,
            config=config,
            source="nflverse_historical_archive",
            dataset_version=dataset.dataset_version(),
            league="nfl",
            market_baseline_name="devigged_reported_close",
        )
    else:
        games, _ = load_settled_games(league=args.league)
        report = build_market_blend_report(
            games,
            market_home_probabilities(league=args.league),
            config=config,
            source="collected_settled_games",
            dataset_version=f"collected_settled_games:{args.league or 'all'}:{len(games)}",
            league=args.league,
        )

    comparisons = report.get("comparisons") or []
    if not comparisons:
        print("No comparison was produced, so there is nothing to audit.")
        return 0

    # The volume has to describe the league the comparison was actually built
    # from. Falling back to NFL for an NBA run would quote the wrong number of
    # seasons and record `league=nfl` against NBA evidence, so an unknown league
    # is refused rather than defaulted.
    if args.pooled:
        volume = combined_volume(PUBLISHED_SCHEDULES)
    else:
        league = report.get("league")
        resolved = volume_for_league(league)
        if resolved is None:
            known = ", ".join(entry.league for entry in PUBLISHED_SCHEDULES)
            target = league or "every league at once"
            print(
                f"No season volume is known for {target}, so the wait cannot be priced.\n"
                f"Re-run with --league one of: {known}, or --pooled to price the basket."
            )
            return 1
        volume = resolved
    audit = audit_detectability(comparisons[0], volume=volume)
    audit["dataset_version"] = report.get("dataset_version")
    audit["model_version"] = comparisons[0].get("model")

    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(audit, indent=2, default=json_default), encoding="utf-8")
        print(f"Wrote {args.output}")
    print(render_power_audit_report(audit))

    if audit.get("status") == "audited":
        # The quote count is the number most likely to be mistaken for evidence,
        # so it is priced in the same breath as the game count.
        inflation = quote_inflation(
            gradable_games=volume.gradable_games_per_season(), quotes_per_game=args.quotes_per_game
        )
        print("")
        print(
            f"One season's quotes at {args.quotes_per_game:g} books per game:"
            f" {inflation['raw_quote_count']:,.0f} prices,"
            f" {inflation['effective_sample']:,.0f} independent outcomes"
            f" ({inflation['inflation_factor']:.0f}x inflation if counted as bets)."
        )

    if args.record:
        entry = record_power_audit_experiment(audit)
        print("")
        if entry is None:
            print("Not recorded: the audit produced no verdict.")
            return 0
        print(f"Recorded: {entry['decision']} ({entry['entry_hash']})")
    return 0


def run_source_probe(args: argparse.Namespace) -> int:
    """Make one request to a public source and report what the normalizer saw.

    Several connectors were written against published documentation rather than
    against a live response, because the environment they were developed in could
    not reach their hosts. This command is how that gap gets closed: it names the
    fields each normalizer depends on and whether the real response carried them,
    instead of failing later with a KeyError inside a collection cycle.
    """
    source = str(args.source).strip().lower()
    if source == "polymarket":
        from .connectors.polymarket import probe_polymarket, render_polymarket_probe

        report = probe_polymarket(limit=args.limit)
        rendered = render_polymarket_probe(report)
    elif source in {"mlb", "nhl"}:
        from .connectors.league_feeds import probe_league_feed, render_league_probe

        report = probe_league_feed(source, date=args.date)
        rendered = render_league_probe(report)
    elif source == "nflverse":
        from .connectors.http import live_probe_client
        from .connectors.nflverse import load_nflverse_games, summarize_dataset

        try:
            dataset = load_nflverse_games(client=live_probe_client(), require_live=True)
        except Exception as exc:  # noqa: BLE001 - the probe reports, it does not raise
            report = {
                "source": "nflverse",
                "reachable": False,
                "error": type(exc).__name__,
                "error_detail": str(exc)[:200],
            }
            rendered = f"nflverse probe: unreachable ({type(exc).__name__}: {str(exc)[:120]})."
        else:
            report = {"source": "nflverse", "reachable": True, **summarize_dataset(dataset)}
            seasons = report.pop("seasons", {})
            report["season_count"] = len(seasons)
            rendered = (
                "nflverse probe\n"
                f"  url: {report.get('source_url')}\n"
                f"  games loaded: {report.get('games_loaded')} across {report.get('season_count')} seasons\n"
                f"  with closing market: {report.get('games_with_closing_market')}\n"
                f"  content hash: {report.get('content_hash')}"
            )
    else:
        print(f"Unknown source: {source}. Known sources: polymarket, mlb, nhl, nflverse.")
        return 2

    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(report, indent=2, default=json_default), encoding="utf-8")
        print(f"Wrote {args.output}")
    print(rendered)
    return 0 if report.get("reachable") else 1


def run_sports_report(args: argparse.Namespace) -> int:
    report = build_sports_report(run_id=args.run_id)
    if args.output:
        write_sports_report(report, args.output)
        print(f"Wrote {args.output}")
    print(render_sports_report(report))
    return 0


def run_sports_record_status(args: argparse.Namespace) -> int:
    report = build_sports_report(run_id=args.run_id)
    ledger_path = args.output or str(default_sports_validation_ledger_path(args.run_id))
    entry = append_sports_validation_ledger(report, path=ledger_path)
    report["validation_ledger_path"] = ledger_path
    report["validation_ledger_status"] = "recorded"
    report["latest_validation_record"] = entry
    if args.report:
        write_sports_report(report, args.report)
        print(f"Wrote report: {args.report}")
    print(f"Recorded sports validation ledger: {ledger_path}")
    print(f"Valid sports predictions total: {entry['valid_sports_predictions_total']}")
    print(f"Settled de-duped exposures: {entry['deduped_settled_exposures']}")
    print(f"Rejected rows: {entry['rejected_rows']}")
    print(f"Win rate: {entry['win_rate'] if entry['win_rate'] is not None else entry['win_rate_status']}")
    if args.tail:
        rows = read_sports_validation_ledger(ledger_path, limit=args.tail)
        print(f"Ledger rows shown: {len(rows)}")
        for row in rows:
            print(
                f"- {row.get('recorded_at')} valid={row.get('valid_sports_predictions_total')} "
                f"settled_deduped={row.get('deduped_settled_exposures')} "
                f"rejected={row.get('rejected_rows')} win_rate_status={row.get('win_rate_status')}"
            )
    return 0


def run_sports_cycle(args: argparse.Namespace) -> int:
    result = sports_cycle(run_id=args.run_id, output=args.output, finals=args.finals)
    print(f"Run: {args.run_id}")
    print(f"Payload: {result['payload_path']}")
    if result["log_result"].get("blocker"):
        print(f"Blocker: {result['log_result']['blocker']}")
    print(f"Logged predictions: {result['log_result']['logged_predictions']}")
    print(f"Rejected predictions: {result['log_result']['rejected_predictions']}")
    print(f"Settled rows: {result['settle_result']['rows_updated']}")
    print(f"Validation ledger: {result['report'].get('validation_ledger_path')}")
    print(f"Gate: {result['report']['gate_result']}")
    return 0


def run_sports_export_features(args: argparse.Namespace) -> int:
    result = export_sports_features(run_id=args.run_id, output=args.output, labels_output=args.labels_output)
    print(f"Feature rows: {result['feature_rows']}")
    print(f"Label rows: {result['label_rows']}")
    print(f"Wrote {result['output']}")
    if result.get("labels_output"):
        print(f"Wrote {result['labels_output']}")
    return 0


def run_connectors_status(args: argparse.Namespace) -> int:
    print(render_connectors_status(build_connectors_status()))
    return 0


def run_archive_reports(args: argparse.Namespace) -> int:
    paths = [Path(path) for path in args.paths] if args.paths else default_report_paths()
    result = archive_files(paths)
    print(f"Archive status: {result['status']}")
    print(f"Uploaded: {result['uploaded_count']}")
    print(f"Failed: {result['failed_count']}")
    return 0


def run_sync_status(args: argparse.Namespace) -> int:
    report: dict[str, Any] | None = None
    if args.run_id and args.asset_class == "crypto":
        report = build_crypto_report(run_id=args.run_id)
    elif args.run_id and args.asset_class == "sports":
        report = build_sports_report(run_id=args.run_id)
    elif args.run_id and args.asset_class == "kalshi":
        report = build_daily_report(create_store(), run_id=args.run_id)
    payloads = {"bot_runs": []}
    if report:
        payloads["bot_runs"].append(
            bot_run_payload(
                report,
                bot_name=args.bot_name or args.asset_class,
                asset_class=args.asset_class,
                stage=args.stage,
                mode="private_research",
            )
        )
    result = sync_status(payloads)
    print(f"Airtable status: {result['status']}")
    print(f"Synced: {result['synced_count']}")
    return 0


def run_send_alert_test(args: argparse.Namespace) -> int:
    alert = build_alert_payload(
        bot_name=args.bot_name,
        asset_class=args.asset_class,
        run_id=args.run_id,
        severity=args.severity,
        event_type="connector_test",
        message="Private research bot connector test alert.",
        report_path=args.report_path,
        next_action="confirm Slack delivery only if alerts are enabled",
    )
    result = send_alert(alert)
    print(f"Slack status: {result['status']}")
    print(f"Sent: {result['sent']}")
    return 0


def run_daemon_status(args: argparse.Namespace) -> int:
    status = build_daemon_status(
        dashboard_url=args.dashboard_url,
        crypto_run_id=args.crypto_run_id,
        sports_run_id=args.sports_run_id,
        kalshi_run_id=args.kalshi_run_id,
    )
    print(render_daemon_status(status))
    return 0


def run_data_quality(args: argparse.Namespace) -> int:
    report = build_data_quality_report(
        dashboard_payload_path=args.dashboard_payload,
        audit_path=args.audit_path,
        error_path=args.error_path,
        crypto_run_id=args.crypto_run_id,
        sports_run_id=args.sports_run_id,
        kalshi_run_id=args.kalshi_run_id,
    )
    if args.output:
        write_data_quality_report(report, args.output, args.json_output)
        print(f"Wrote {args.output}")
        if args.json_output:
            print(f"Wrote {args.json_output}")
    print(render_data_quality_report(report))
    return 0


def run_company_status(args: argparse.Namespace) -> int:
    print(render_bot_company(bot_company_summary()))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kalshi-research-bot")
    subparsers = parser.add_subparsers(dest="command", required=True)

    demo = subparsers.add_parser("demo", help="run the sample research pipeline")
    demo.add_argument("--min-edge", type=float, default=0.0)
    demo.add_argument("--save-db", action="store_true")
    demo.set_defaults(func=run_demo)

    run = subparsers.add_parser("run", help="run research with local game and quote JSON")
    run.add_argument("--games", required=True)
    run.add_argument("--quotes", required=True)
    run.add_argument("--min-edge", type=float, default=0.0)
    run.add_argument("--save-db", action="store_true")
    run.set_defaults(func=run_research)

    collect = subparsers.add_parser("collect", help="collect enabled sources from a source config")
    collect.add_argument("--sources", default=str(repo_path("config", "sources.example.json")))
    collect.add_argument("--save-db", action="store_true")
    collect.set_defaults(func=run_collect)

    combo = subparsers.add_parser("combo", help="rank over/under combos by combined probability")
    combo.add_argument("--legs", default=str(repo_path("examples", "sample_totals.json")))
    combo.add_argument("--target", type=float, default=0.80)
    combo.add_argument("--min-legs", type=int, default=2)
    combo.add_argument("--max-legs", type=int, default=5)
    combo.add_argument("--max-results", type=int, default=20)
    combo.add_argument("--min-leg-probability", type=float, default=0.75)
    combo.set_defaults(func=run_combo)

    today = subparsers.add_parser("today", help="fetch today's public schedule and Kalshi combo markets")
    today.add_argument("--date", help="date as YYYYMMDD; defaults to local today")
    today.add_argument("--output", default=str(repo_path("data", "today_paper_view.json")))
    today.add_argument("--public-intel", help="optional JSON file of public bettor/trader signals")
    today.set_defaults(func=run_today)

    paper = subparsers.add_parser("paper", help="serve the local paper-view dashboard")
    paper.add_argument("--host", default="127.0.0.1")
    paper.add_argument("--port", type=int, default=8765)
    paper.add_argument("--date", help="date as YYYYMMDD; defaults to local today")
    paper.add_argument("--output", default=str(repo_path("data", "today_paper_view.json")))
    paper.add_argument("--refresh-seconds", type=int, default=600)
    paper.add_argument("--target", type=float, default=0.80, help="minimum individual leg probability")
    paper.add_argument("--min-legs", type=int, default=8)
    paper.add_argument("--max-legs", type=int, default=20)
    paper.add_argument("--min-leg-probability", type=float, default=None, help="override --target for each leg")
    paper.add_argument("--max-leg-probability", type=float, default=0.985)
    paper.add_argument("--leverage-target", type=float, default=0.75, help="minimum individual leg probability for leverage slip")
    paper.add_argument("--public-intel", help="optional JSON file of public bettor/trader signals")
    paper.add_argument("--stake", type=float, default=5.0)
    paper.set_defaults(func=run_paper)

    database_status = subparsers.add_parser("database-status", help="private database readiness summary")
    database_status.set_defaults(func=run_database_status)

    preflight = subparsers.add_parser(
        "preflight",
        help="check every deployment gate against this environment, read-only",
    )
    preflight.add_argument("--json", action="store_true", help="emit the report as JSON")
    preflight.set_defaults(func=run_preflight)

    database_migrate = subparsers.add_parser("database-migrate", help="apply versioned PostgreSQL migrations")
    database_migrate.set_defaults(func=run_database_migrate)

    auth_create = subparsers.add_parser("auth-create-user", help="create a private local dashboard account when enabled")
    auth_create.add_argument("--username", required=True)
    auth_create.add_argument("--role", choices=["admin", "researcher", "read_only"], required=True)
    auth_create.set_defaults(func=run_auth_create_user)

    auth_disable = subparsers.add_parser("auth-disable-user", help="disable or re-enable a private dashboard account")
    auth_disable.add_argument("--username", required=True)
    auth_disable.add_argument("--enable", action="store_true")
    auth_disable.set_defaults(func=run_auth_disable_user)

    operator_add = subparsers.add_parser(
        "operator-message-add",
        help="queue a private Codex/operator instruction from a local UTF-8 file",
    )
    operator_add.add_argument("--title", required=True)
    operator_add.add_argument("--file", required=True, help="instruction body file; avoids shell-history exposure")
    operator_add.add_argument("--created-by", default=os.environ.get("OPERATOR_NAME", "owner"))
    operator_add.add_argument("--priority", choices=PRIORITIES, default="normal")
    operator_add.add_argument("--target", choices=TARGETS, default="codex")
    operator_add.add_argument("--message-id")
    operator_add.set_defaults(func=run_operator_message_add)

    operator_list = subparsers.add_parser(
        "operator-message-list",
        help="list the private manual-review instruction queue",
    )
    operator_list.add_argument("--status", choices=STATUSES)
    operator_list.add_argument("--target", choices=TARGETS)
    operator_list.add_argument("--limit", type=int, default=100)
    operator_list.set_defaults(func=run_operator_message_list)

    operator_claim = subparsers.add_parser(
        "operator-message-claim",
        help="mark one private instruction as claimed without executing it",
    )
    operator_claim.add_argument("--message-id", required=True)
    operator_claim.add_argument("--agent", default="codex")
    operator_claim.set_defaults(func=run_operator_message_claim)

    operator_complete = subparsers.add_parser(
        "operator-message-complete",
        help="record a reviewed instruction result from a local UTF-8 summary file",
    )
    operator_complete.add_argument("--message-id", required=True)
    operator_complete.add_argument("--summary-file", required=True)
    operator_complete.add_argument("--agent", default="codex")
    operator_complete.set_defaults(func=run_operator_message_complete)

    worker = subparsers.add_parser("worker", help="run one isolated private research worker service")
    worker.add_argument("--service", choices=sorted(SERVICE_SPECS), required=True)
    worker.add_argument("--once", action="store_true")
    worker.add_argument("--idempotency-key")
    worker.add_argument("--kalshi-run-id", default=os.environ.get("KALSHI_RUN_ID", "stage3a_20260703_170707"))
    worker.add_argument("--crypto-run-id", default=os.environ.get("CRYPTO_RUN_ID", "crypto_private_20260704"))
    worker.add_argument("--sports-run-id", default=os.environ.get("SPORTS_RUN_ID", "sports_private_20260704"))
    worker.set_defaults(func=run_worker_command)

    worker_status = subparsers.add_parser("worker-status", help="private worker/database/model status JSON")
    worker_status.set_defaults(func=run_worker_status)

    hosted_service = subparsers.add_parser(
        "service-start",
        help="start the Railway web or isolated worker role selected by HAWKNETIC_SERVICE",
    )
    hosted_service.set_defaults(func=run_hosted_service)

    pick = subparsers.add_parser("pick", help="generate a validated research candidate, no-bet, or wait decision")
    pick.add_argument("--date", help="date as YYYYMMDD; defaults to local today")
    pick.add_argument("--output", default=str(repo_path("data", "today_paper_view.json")))
    pick.set_defaults(func=run_pick)

    slip = subparsers.add_parser("slip", help="build a fresh exact-listed research watch slip from market-implied legs")
    slip.add_argument("--date", help="date as YYYYMMDD; defaults to local today")
    slip.add_argument("--output", default=str(repo_path("data", "today_paper_view.json")))
    slip.add_argument("--target", type=float, default=0.80, help="minimum individual market-implied probability")
    slip.add_argument("--min-legs", type=int, default=8)
    slip.add_argument("--max-legs", type=int, default=20)
    slip.add_argument("--min-leg-probability", type=float, default=None, help="override --target for each leg")
    slip.add_argument("--max-leg-probability", type=float, default=0.985)
    slip.add_argument("--public-intel", help="optional JSON file of public bettor/trader signals")
    slip.add_argument("--stake", type=float, default=5.0)
    slip.set_defaults(func=run_slip)

    backtest = subparsers.add_parser("backtest", help="replay historical pre-event prediction snapshots")
    backtest.add_argument("--input", default=str(repo_path("examples", "backtest_sample.json")))
    backtest.add_argument("--output", default=str(repo_path("data", "backtest_report.txt")))
    backtest.set_defaults(func=run_backtest_command)

    paper_run_start = subparsers.add_parser("paper-run-start", help="start a private Stage 3A paper-test run")
    paper_run_start.add_argument("--run-id")
    paper_run_start.add_argument("--lock-path", default=None)
    paper_run_start.set_defaults(func=lambda args: _paper_run_start_with_defaults(args))

    paper_log = subparsers.add_parser("paper-log", help="log private forward-only paper predictions from a payload")
    paper_log.add_argument("--run-id", required=True)
    paper_log.add_argument("--input", default=str(repo_path("data", "today_paper_view.json")))
    paper_log.add_argument("--date")
    paper_log.add_argument("--report")
    paper_log.set_defaults(func=lambda args: _paper_log_with_defaults(args))

    paper_settle = subparsers.add_parser("paper-settle", help="import private paper settlement outcomes")
    paper_settle.add_argument("--run-id", required=True)
    paper_settle.add_argument("--settlements", required=True)
    paper_settle.add_argument("--date")
    paper_settle.add_argument("--report")
    paper_settle.set_defaults(func=lambda args: _paper_settle_with_defaults(args))

    paper_settle_kalshi = subparsers.add_parser("paper-settle-kalshi", help="import official Kalshi settlement data for a paper run")
    paper_settle_kalshi.add_argument("--run-id", required=True)
    paper_settle_kalshi.add_argument("--date")
    paper_settle_kalshi.add_argument("--report")
    paper_settle_kalshi.set_defaults(func=lambda args: _paper_settle_kalshi_with_defaults(args))

    paper_report = subparsers.add_parser("paper-report", help="render a private Stage 3A daily paper-test report")
    paper_report.add_argument("--run-id", required=True)
    paper_report.add_argument("--date")
    paper_report.add_argument("--output")
    paper_report.set_defaults(func=lambda args: _paper_report_with_defaults(args))

    paper_stage3b = subparsers.add_parser("paper-stage3b-audit", help="render a private Stage 3B settled performance audit")
    paper_stage3b.add_argument("--run-id", required=True)
    paper_stage3b.add_argument("--output")
    paper_stage3b.set_defaults(func=lambda args: _paper_stage3b_audit_with_defaults(args))

    kalshi_return_audit = subparsers.add_parser(
        "kalshi-return-audit",
        help="render private Kalshi fee, execution, and correlated-exposure decomposition",
    )
    kalshi_return_audit.add_argument("--run-id", required=True)
    kalshi_return_audit.add_argument("--output")
    kalshi_return_audit.set_defaults(func=lambda args: _kalshi_return_audit_with_defaults(args))

    model_evaluate = subparsers.add_parser(
        "model-evaluate",
        help="evaluate category-specific research probabilities against time-aware baselines",
    )
    model_evaluate.add_argument("--kalshi-run-id", default=os.environ.get("KALSHI_RUN_ID", "stage3a_20260703_170707"))
    model_evaluate.add_argument("--crypto-run-id", default=os.environ.get("CRYPTO_RUN_ID", "crypto_private_20260704"))
    model_evaluate.add_argument("--sports-run-id", default=os.environ.get("SPORTS_RUN_ID", "sports_private_20260704"))
    model_evaluate.add_argument("--output")
    model_evaluate.add_argument("--no-persist", action="store_true")
    model_evaluate.set_defaults(func=lambda args: _model_evaluate_with_defaults(args))

    crypto_collect = subparsers.add_parser("crypto-collect", help="private crypto source collection")
    crypto_collect.add_argument("--output", default=str(repo_path("data", "crypto_runs", "latest_source.json")))
    crypto_collect.set_defaults(func=run_crypto_collect)

    crypto_log = subparsers.add_parser("crypto-log", help="log private crypto research predictions")
    crypto_log.add_argument("--run-id", required=True)
    crypto_log.add_argument("--input", default=str(repo_path("data", "crypto_runs", "latest_source.json")))
    crypto_log.add_argument("--report")
    crypto_log.set_defaults(func=lambda args: _crypto_log_with_defaults(args))

    crypto_settle = subparsers.add_parser("crypto-settle", help="settle eligible private crypto predictions")
    crypto_settle.add_argument("--run-id", required=True)
    crypto_settle.add_argument("--input", default=str(repo_path("data", "crypto_runs", "latest_source.json")))
    crypto_settle.add_argument("--report")
    crypto_settle.set_defaults(func=lambda args: _crypto_settle_with_defaults(args))

    crypto_report = subparsers.add_parser("crypto-report", help="render private crypto research report")
    crypto_report.add_argument("--run-id", required=True)
    crypto_report.add_argument("--output")
    crypto_report.set_defaults(func=lambda args: _crypto_report_with_defaults(args))

    crypto_stage3b = subparsers.add_parser("crypto-stage3b-audit", help="render private crypto Stage 3B settled performance audit")
    crypto_stage3b.add_argument("--run-id", required=True)
    crypto_stage3b.add_argument("--output")
    crypto_stage3b.set_defaults(func=lambda args: _crypto_stage3b_audit_with_defaults(args))

    crypto_stage4 = subparsers.add_parser("crypto-stage4-diagnostic", help="render private crypto Stage 4 controlled diagnostic report")
    crypto_stage4.add_argument("--run-id", required=True)
    crypto_stage4.add_argument("--output")
    crypto_stage4.set_defaults(func=lambda args: _crypto_stage4_diagnostic_with_defaults(args))

    crypto_cycle_cmd = subparsers.add_parser("crypto-cycle", help="private crypto collect/log/settle/report cycle")
    crypto_cycle_cmd.add_argument("--run-id", required=True)
    crypto_cycle_cmd.add_argument("--output")
    crypto_cycle_cmd.set_defaults(func=lambda args: _crypto_cycle_with_defaults(args))

    crypto_export = subparsers.add_parser("crypto-export-features", help="export crypto ML-ready features without leakage")
    crypto_export.add_argument("--run-id", required=True)
    crypto_export.add_argument("--output")
    crypto_export.add_argument("--labels-output")
    crypto_export.set_defaults(func=lambda args: _crypto_export_features_with_defaults(args))

    sports_collect = subparsers.add_parser("sports-collect", help="private sports odds source collection")
    sports_collect.add_argument("--sport", default="baseball_mlb")
    sports_collect.add_argument("--output", default=str(repo_path("data", "sports_runs", "latest_odds.json")))
    sports_collect.set_defaults(func=run_sports_collect)

    sports_log = subparsers.add_parser("sports-log", help="log private sports odds research predictions")
    sports_log.add_argument("--run-id", required=True)
    sports_log.add_argument("--input", default=str(repo_path("data", "sports_runs", "latest_odds.json")))
    sports_log.add_argument("--report")
    sports_log.set_defaults(func=lambda args: _sports_log_with_defaults(args))

    sports_settle = subparsers.add_parser("sports-settle", help="settle private sports predictions from official final scores")
    sports_settle.add_argument("--run-id", required=True)
    sports_settle.add_argument("--finals", required=True)
    sports_settle.add_argument("--report")
    sports_settle.set_defaults(func=lambda args: _sports_settle_with_defaults(args))

    sports_report = subparsers.add_parser("sports-report", help="render private sports research report")
    sports_report.add_argument("--run-id", required=True)
    sports_report.add_argument("--output")
    sports_report.set_defaults(func=lambda args: _sports_report_with_defaults(args))

    sports_record = subparsers.add_parser("sports-record-status", help="append latest sports research metrics to the validation ledger")
    sports_record.add_argument("--run-id", required=True)
    sports_record.add_argument("--output")
    sports_record.add_argument("--report")
    sports_record.add_argument("--tail", type=int, default=0)
    sports_record.set_defaults(func=lambda args: _sports_record_status_with_defaults(args))

    sports_cycle_cmd = subparsers.add_parser("sports-cycle", help="private sports collect/log/settle/report cycle")
    sports_cycle_cmd.add_argument("--run-id", required=True)
    sports_cycle_cmd.add_argument("--output")
    sports_cycle_cmd.add_argument("--finals")
    sports_cycle_cmd.set_defaults(func=lambda args: _sports_cycle_with_defaults(args))

    raw_retention = subparsers.add_parser(
        "raw-retention",
        help="report raw payload storage and age out payload bodies past a window",
    )
    raw_retention.add_argument("--older-than-days", type=int, default=30, help="retention window in days (minimum 7)")
    raw_retention.add_argument("--source", default=None, help="limit to one collection source")
    raw_retention.add_argument("--limit", type=int, default=5000, help="maximum rows pruned in one pass")
    raw_retention.add_argument("--apply", action="store_true", help="write the changes; omit for a dry run")
    raw_retention.add_argument("--report-only", action="store_true", help="show storage usage without evaluating a prune")
    raw_retention.set_defaults(func=run_raw_retention)

    sports_clv = subparsers.add_parser(
        "sports-clv",
        help="record closing lines for started games and report closing line value",
    )
    sports_clv.add_argument("--run-id", default=None, help="limit to one run; omit to cover every run")
    sports_clv.add_argument("--output", help="write the report JSON to this path")
    sports_clv.add_argument("--limit", type=int, default=25, help="maximum bookmakers in the breakdown")
    sports_clv.add_argument(
        "--report-only",
        action="store_true",
        help="report stored closing line value without recording new closes",
    )
    sports_clv.set_defaults(func=run_sports_clv)

    sports_ratings = subparsers.add_parser(
        "sports-ratings",
        help="rate teams from settled games and grade the rating against its baselines",
    )
    sports_ratings.add_argument("--league", default=None, help="limit to one league; omit to cover every league")
    sports_ratings.add_argument("--since", default=None, help="ignore games starting before this ISO timestamp")
    sports_ratings.add_argument("--k-factor", type=float, default=20.0, help="Elo update size")
    sports_ratings.add_argument("--home-advantage", type=float, default=55.0, help="home edge in rating points")
    sports_ratings.add_argument(
        "--min-team-games",
        type=int,
        default=5,
        help="games a team must have played before its forecasts are scored",
    )
    sports_ratings.add_argument(
        "--min-games",
        type=int,
        default=30,
        help="evaluated games required before a verdict is stated",
    )
    sports_ratings.add_argument(
        "--historical",
        action="store_true",
        help="grade against the public nflverse archive instead of collected rows",
    )
    sports_ratings.add_argument(
        "--seasons",
        default=None,
        help="historical only: limit to these seasons, e.g. 2015-2025 written as '2015 2016 ...'",
    )
    sports_ratings.add_argument(
        "--regular-season-only",
        action="store_true",
        help="historical only: exclude playoff games",
    )
    sports_ratings.add_argument("--output", help="write the report JSON to this path")
    sports_ratings.add_argument(
        "--record",
        action="store_true",
        help="append the verdict to the research registry, including a negative one",
    )
    sports_ratings.set_defaults(func=run_sports_ratings)

    source_probe = subparsers.add_parser(
        "source-probe",
        help="make one request to a public source and report what its normalizer saw",
    )
    source_probe.add_argument(
        "source",
        help="polymarket, mlb, nhl, or nflverse",
    )
    source_probe.add_argument("--date", default=None, help="league feeds: the day to fetch (YYYY-MM-DD)")
    source_probe.add_argument("--limit", type=int, default=25, help="polymarket: markets to request")
    source_probe.add_argument("--output", help="write the probe JSON to this path")
    source_probe.set_defaults(func=run_source_probe)

    venue_compare = subparsers.add_parser(
        "venue-compare",
        help="compare the sportsbook board against Polymarket on the games both price",
    )
    venue_compare.add_argument("--limit", type=int, default=250, help="polymarket markets to request")
    venue_compare.add_argument("--top", type=int, default=15, help="widest gaps to print")
    venue_compare.add_argument(
        "--start-tolerance-minutes",
        type=int,
        default=120,
        help="how far apart two venues' start times may be and still be one game",
    )
    venue_compare.add_argument("--output", help="write the comparison JSON to this path")
    venue_compare.set_defaults(func=run_venue_compare)

    market_blend = subparsers.add_parser(
        "market-blend",
        help="test whether the model adds anything to the closing price it starts from",
    )
    market_blend.add_argument("--league", default=None, help="limit to one league")
    market_blend.add_argument(
        "--historical",
        action="store_true",
        help="grade against the public nflverse archive instead of collected rows",
    )
    market_blend.add_argument("--seasons", default=None, help="historical only: limit to these seasons")
    market_blend.add_argument(
        "--regular-season-only", action="store_true", help="historical only: exclude playoff games"
    )
    market_blend.add_argument(
        "--min-training-rows",
        type=int,
        default=300,
        help="games of earlier history a refit needs before its output is scored",
    )
    market_blend.add_argument(
        "--min-team-games",
        type=int,
        default=5,
        help="games a team must have played before its forecasts are scored",
    )
    market_blend.add_argument("--output", help="write the report JSON to this path")
    market_blend.add_argument(
        "--record", action="store_true", help="append the verdict to the research registry"
    )
    market_blend.set_defaults(func=run_market_blend)

    power_audit = subparsers.add_parser(
        "power-audit",
        help="report what the evidence on hand could ever have detected (E-09)",
    )
    power_audit.add_argument("--league", default=None, help="limit to one league")
    power_audit.add_argument(
        "--historical",
        action="store_true",
        help="audit the public nflverse archive instead of collected rows",
    )
    power_audit.add_argument("--seasons", default=None, help="historical only: limit to these seasons")
    power_audit.add_argument(
        "--regular-season-only", action="store_true", help="historical only: exclude playoff games"
    )
    power_audit.add_argument(
        "--min-team-games",
        type=int,
        default=5,
        help="games a team must have played before its forecasts are scored",
    )
    power_audit.add_argument(
        "--pooled",
        action="store_true",
        help="price the wait against every league pooled, not NFL alone",
    )
    power_audit.add_argument(
        "--quotes-per-game",
        type=float,
        default=5.0,
        help="books quoting each game, for the quote-inflation figure",
    )
    power_audit.add_argument("--output", help="write the audit JSON to this path")
    power_audit.add_argument(
        "--record", action="store_true", help="append the verdict to the research registry"
    )
    power_audit.set_defaults(func=run_power_audit)

    slip_analyze = subparsers.add_parser(
        "slip-analyze",
        help="grade a multi-leg slip against its own break-even, with correlation",
    )
    slip_analyze.add_argument("--legs", required=True, help="JSON file of leg objects")
    slip_analyze.add_argument("--stake", type=float, default=1.0, help="stake for payout and EV")
    slip_analyze.add_argument(
        "--draws", type=int, default=10_000, help="Monte Carlo draws (default 10,000)"
    )
    slip_analyze.add_argument(
        "--trim", action="store_true", help="also report which legs to drop"
    )
    slip_analyze.add_argument("--json", action="store_true", help="emit the full report as JSON")
    slip_analyze.set_defaults(func=run_slip_analyze)

    devig = subparsers.add_parser(
        "devig-compare",
        help="compare margin-removal methods for one market and report their disagreement",
    )
    devig_prices = devig.add_mutually_exclusive_group(required=True)
    devig_prices.add_argument("--american", type=float, nargs="+", help="American prices, one per selection")
    devig_prices.add_argument("--decimal", type=float, nargs="+", help="decimal prices, one per selection")
    devig.add_argument("--output", help="write the comparison JSON to this path")
    devig.set_defaults(func=run_devig_compare)

    research_power = subparsers.add_parser(
        "research-power",
        help="how many predictions a claim needs, or the smallest edge a sample could detect",
    )
    research_power.add_argument("--edge", type=float, default=None, help="edge over break-even, e.g. 0.01")
    research_power.add_argument("--sample-size", type=int, default=None, help="resolved predictions available")
    research_power.add_argument("--score-improvement", type=float, default=None, help="paired Brier or log-loss gain")
    research_power.add_argument("--score-std", type=float, default=0.05, help="standard deviation of the paired difference")
    research_power.add_argument("--alpha", type=float, default=0.05)
    research_power.add_argument("--power", type=float, default=0.80)
    research_power.add_argument("--break-even", type=float, default=110.0 / 210.0, help="break-even probability, default -110")
    research_power.add_argument("--cluster-size", type=float, default=1.0, help="correlated predictions per group")
    research_power.add_argument("--intraclass-correlation", type=float, default=0.0)
    research_power.set_defaults(func=run_research_power)

    research_registry_cmd = subparsers.add_parser(
        "research-registry",
        help="summarize recorded experiments and apply family-wise correction",
    )
    research_registry_cmd.add_argument("--path", default=None, help="registry path; defaults to data/research")
    research_registry_cmd.add_argument("--fdr", type=float, default=0.05, help="false discovery rate")
    research_registry_cmd.add_argument("--negative-results", action="store_true", help="list rejected hypotheses")
    research_registry_cmd.add_argument("--output", help="write the review JSON to this path")
    research_registry_cmd.set_defaults(func=run_research_registry)

    sports_export = subparsers.add_parser("sports-export-features", help="export sports ML-ready features without leakage")
    sports_export.add_argument("--run-id", required=True)
    sports_export.add_argument("--output")
    sports_export.add_argument("--labels-output")
    sports_export.set_defaults(func=lambda args: _sports_export_features_with_defaults(args))

    connectors_status = subparsers.add_parser("connectors-status", help="private connector configuration/status summary")
    connectors_status.set_defaults(func=run_connectors_status)

    archive_reports = subparsers.add_parser("archive-reports", help="archive private reports to Google Drive when enabled")
    archive_reports.add_argument("paths", nargs="*")
    archive_reports.set_defaults(func=run_archive_reports)

    sync_status_cmd = subparsers.add_parser("sync-status", help="sync private bot status to Airtable when enabled")
    sync_status_cmd.add_argument("--asset-class", choices=["crypto", "sports", "kalshi"], default="crypto")
    sync_status_cmd.add_argument("--run-id")
    sync_status_cmd.add_argument("--bot-name")
    sync_status_cmd.add_argument("--stage", default="Stage 3A")
    sync_status_cmd.set_defaults(func=run_sync_status)

    alert_test = subparsers.add_parser("send-alert-test", help="send a private Slack connector test alert when enabled")
    alert_test.add_argument("--bot-name", default="connector-test")
    alert_test.add_argument("--asset-class", default="system")
    alert_test.add_argument("--run-id", default="connector_test")
    alert_test.add_argument("--severity", default="info")
    alert_test.add_argument("--report-path")
    alert_test.set_defaults(func=run_send_alert_test)

    daemon_status = subparsers.add_parser("daemon-status", help="private always-on scheduler/watchdog status")
    daemon_status.add_argument("--dashboard-url", default="http://127.0.0.1:8765")
    daemon_status.add_argument("--crypto-run-id", default="crypto_private_20260704")
    daemon_status.add_argument("--sports-run-id", default="sports_private_20260704")
    daemon_status.add_argument("--kalshi-run-id", default="stage3a_20260703_170707")
    daemon_status.set_defaults(func=run_daemon_status)

    data_quality = subparsers.add_parser("data-quality", help="private source quality and metric contamination audit")
    data_quality.add_argument("--dashboard-payload", default=str(repo_path("data", "today_paper_view.json")))
    data_quality.add_argument("--audit-path", default=str(repo_path("data", "refresh_audit.jsonl")))
    data_quality.add_argument("--error-path", default=str(repo_path("data", "error_events.jsonl")))
    data_quality.add_argument("--crypto-run-id", default="crypto_private_20260704")
    data_quality.add_argument("--sports-run-id", default="sports_private_20260704")
    data_quality.add_argument("--kalshi-run-id", default="stage3a_20260703_170707")
    data_quality.add_argument("--output", default=str(default_data_quality_report_path()))
    data_quality.add_argument("--json-output", default=str(default_data_quality_json_path()))
    data_quality.set_defaults(func=run_data_quality)

    company_status = subparsers.add_parser("company-status", help="private bot-company roster and cadence plan")
    company_status.set_defaults(func=run_company_status)

    return parser


def _paper_run_start_with_defaults(args: argparse.Namespace) -> int:
    if args.lock_path is None:
        preview_run = start_paper_test_run(
            create_store(),
            run_id=args.run_id,
            lock_path=None,
        )
        args.run_id = preview_run["run_id"]
        args.lock_path = str(default_run_lock_path(args.run_id))
        Path(args.lock_path).parent.mkdir(parents=True, exist_ok=True)
        Path(args.lock_path).write_text(__import__("json").dumps(preview_run, indent=2, sort_keys=True), encoding="utf-8")
        print(f"Started private paper run: {preview_run['run_id']}")
        print(f"Config hash: {preview_run['config_hash']}")
        print(f"Lock file: {args.lock_path}")
        return 0
    return run_paper_run_start(args)


def _paper_log_with_defaults(args: argparse.Namespace) -> int:
    if args.report is None:
        args.report = str(default_daily_report_path(args.run_id))
    return run_paper_log(args)


def _paper_settle_with_defaults(args: argparse.Namespace) -> int:
    if args.report is None:
        args.report = str(default_daily_report_path(args.run_id))
    return run_paper_settle(args)


def _paper_settle_kalshi_with_defaults(args: argparse.Namespace) -> int:
    if args.report is None:
        args.report = str(default_daily_report_path(args.run_id))
    return run_paper_settle_kalshi(args)


def _paper_report_with_defaults(args: argparse.Namespace) -> int:
    if args.output is None:
        args.output = str(default_daily_report_path(args.run_id))
    return run_paper_report(args)


def _paper_stage3b_audit_with_defaults(args: argparse.Namespace) -> int:
    if args.output is None:
        args.output = str(default_stage3b_audit_path(args.run_id))
    return run_paper_stage3b_audit(args)


def _kalshi_return_audit_with_defaults(args: argparse.Namespace) -> int:
    if args.output is None:
        args.output = str(default_kalshi_return_decomposition_path(args.run_id))
    return run_kalshi_return_audit(args)


def _model_evaluate_with_defaults(args: argparse.Namespace) -> int:
    if args.output is None:
        args.output = str(default_platform_model_audit_path())
    return run_model_evaluate(args)


def _crypto_log_with_defaults(args: argparse.Namespace) -> int:
    if args.report is None:
        args.report = str(default_crypto_daily_report_path(args.run_id))
    return run_crypto_log(args)


def _crypto_settle_with_defaults(args: argparse.Namespace) -> int:
    if args.report is None:
        args.report = str(default_crypto_daily_report_path(args.run_id))
    return run_crypto_settle(args)


def _crypto_report_with_defaults(args: argparse.Namespace) -> int:
    if args.output is None:
        args.output = str(default_crypto_all_report_path(args.run_id))
    return run_crypto_report(args)


def _crypto_stage3b_audit_with_defaults(args: argparse.Namespace) -> int:
    if args.output is None:
        args.output = str(default_crypto_stage3b_audit_path(args.run_id))
    return run_crypto_stage3b_audit(args)


def _crypto_stage4_diagnostic_with_defaults(args: argparse.Namespace) -> int:
    if args.output is None:
        args.output = str(default_crypto_stage4_diagnostic_path(args.run_id))
    return run_crypto_stage4_diagnostic(args)


def _crypto_cycle_with_defaults(args: argparse.Namespace) -> int:
    if args.output is None:
        args.output = str(default_crypto_payload_path(args.run_id))
    return run_crypto_cycle(args)


def _crypto_export_features_with_defaults(args: argparse.Namespace) -> int:
    if args.output is None:
        args.output = str(default_crypto_features_path(args.run_id))
    if args.labels_output is None:
        args.labels_output = str(default_crypto_labels_path(args.run_id))
    return run_crypto_export_features(args)


def _sports_log_with_defaults(args: argparse.Namespace) -> int:
    if args.report is None:
        args.report = str(default_sports_daily_report_path(args.run_id))
    return run_sports_log(args)


def _sports_settle_with_defaults(args: argparse.Namespace) -> int:
    if args.report is None:
        args.report = str(default_sports_daily_report_path(args.run_id))
    return run_sports_settle(args)


def _sports_report_with_defaults(args: argparse.Namespace) -> int:
    if args.output is None:
        args.output = str(default_sports_all_report_path(args.run_id))
    return run_sports_report(args)


def _sports_record_status_with_defaults(args: argparse.Namespace) -> int:
    if args.output is None:
        args.output = str(default_sports_validation_ledger_path(args.run_id))
    return run_sports_record_status(args)


def run_devig_compare(args: argparse.Namespace) -> int:
    """Show what every margin-removal method makes of one market.

    The number to read is the disagreement: it is the size of the de-vig
    assumption, and an estimated edge smaller than it says more about the method
    than about the game.
    """
    from decimal import Decimal

    from .math.devig import (
        american_odds_to_implied,
        compare_methods,
        decimal_odds_to_implied,
        method_disagreement,
    )

    if args.american:
        implied = [american_odds_to_implied(value) for value in args.american]
    else:
        implied = [decimal_odds_to_implied(value) for value in args.decimal]
    if len(implied) < 2:
        print("At least two selections are required to remove margin.")
        return 2

    results = compare_methods(implied)
    total = sum(implied, Decimal(0))
    print(f"Selections: {len(implied)}")
    print(f"Booksum:    {total:.6f}")
    print(f"Overround:  {total - Decimal(1):.6f}")
    print("")
    width = max(len(name) for name in results)
    for name, result in results.items():
        probabilities = " ".join(f"{value:.4f}" for value in result.probabilities)
        parameter = "" if result.parameter is None else f"  param={result.parameter:.5f}"
        flag = "" if result.converged else "  NOT CONVERGED"
        print(f"  {name:<{width}}  {probabilities}{parameter}{flag}")
        for note in result.notes:
            print(f"  {'':<{width}}  note: {note}")

    disagreement = method_disagreement(implied)
    print("")
    print(f"Largest disagreement between methods: {disagreement * 100:.2f} probability points")
    print("An estimated edge below that figure is a statement about the de-vig")
    print("method, not about the event. This is a baseline, never a recommendation.")
    if args.output:
        payload = {name: result.as_dict() for name, result in results.items()}
        payload["disagreement"] = str(disagreement)
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(payload, indent=2, default=json_default), encoding="utf-8")
        print(f"Wrote {args.output}")
    return 0


def run_research_power(args: argparse.Namespace) -> int:
    """Report how much evidence a claim needs, or what a sample could detect."""
    from .evaluation.power import (
        effective_sample_size,
        minimum_detectable_edge,
        required_sample_for_edge,
        required_sample_for_score_improvement,
    )

    print(f"alpha={args.alpha}  power={args.power}  break-even={args.break_even:.6f}")
    print("")
    if args.edge is not None:
        result = required_sample_for_edge(
            edge=args.edge,
            break_even_probability=args.break_even,
            alpha=args.alpha,
            power=args.power,
        )
        print(f"To detect an edge of {args.edge:.3%} you need {result.required_sample:,} resolved predictions.")
    if args.sample_size is not None:
        detectable = minimum_detectable_edge(
            sample_size=args.sample_size,
            break_even_probability=args.break_even,
            alpha=args.alpha,
            power=args.power,
        )
        print(f"With {args.sample_size:,} resolved predictions the smallest detectable edge is {detectable:.3%}.")
        print("An effect below that is not absent, it is invisible to this sample.")
    if args.score_improvement is not None:
        result = required_sample_for_score_improvement(
            mean_difference=args.score_improvement,
            difference_std=args.score_std,
            alpha=args.alpha,
            power=args.power,
        )
        print(
            f"To show a paired score gain of {args.score_improvement} "
            f"(sd {args.score_std}) you need {result.required_sample:,} predictions."
        )
    if args.cluster_size > 1:
        effective = effective_sample_size(
            sample_size=args.sample_size or 0,
            cluster_size=args.cluster_size,
            intraclass_correlation=args.intraclass_correlation,
        )
        print("")
        print(
            f"Clustered in groups of {args.cluster_size} at rho={args.intraclass_correlation}, "
            f"{args.sample_size or 0:,} predictions are worth {effective:,.1f} independent ones."
        )
    if args.edge is None and args.sample_size is None and args.score_improvement is None:
        print("Provide --edge, --sample-size, or --score-improvement.")
        return 2
    return 0


def run_research_registry(args: argparse.Namespace) -> int:
    """Summarize recorded experiments and re-judge them against the whole family."""
    from .research_registry import (
        default_registry_path,
        negative_results,
        registry_summary,
        significance_review,
    )

    path = Path(args.path) if args.path else default_registry_path()
    summary = registry_summary(path)
    print(f"Registry: {path}")
    print(f"Entries:  {summary['entry_count']} across {summary['distinct_hypotheses']} distinct hypotheses")
    print(
        f"Decisions: accepted={summary['decisions']['accepted']} "
        f"rejected={summary['decisions']['rejected']} inconclusive={summary['decisions']['inconclusive']}"
    )
    print(f"Chain valid: {summary['chain_valid']}")
    if not summary["chain_valid"]:
        print(f"  History was edited or truncated at entry index {summary['broken_at_index']}.")

    if args.negative_results:
        print("")
        print("Rejected hypotheses:")
        rejections = negative_results(path)
        if not rejections:
            print("  (none recorded)")
        for row in rejections:
            print(f"  - {row.get('hypothesis')}  [{row.get('test_method')}, n={row.get('sample_size')}]")

    if summary["entry_count"]:
        review = significance_review(path, false_discovery_rate=args.fdr)
        print("")
        print(f"Family-wise review at FDR={args.fdr}: {review['scored_count']} scored, {review['unscored_count']} unscored")
        print(
            f"Expected false positives without correction: "
            f"{review['expected_false_positives_uncorrected']:.1f}"
        )
        if review["demoted"]:
            print("")
            print("DEMOTED - accepted findings that do not survive correction:")
            for row in review["demoted"]:
                print(f"  - {row['hypothesis']}  p={row['p_value']:.4f} adjusted={row['adjusted_p_value']:.4f}")
            print("Re-run these before citing them as results.")
        else:
            print("No accepted finding was demoted by family-wise correction.")
    if args.output:
        payload = {"summary": summary, "review": significance_review(path, false_discovery_rate=args.fdr)}
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(payload, indent=2, default=json_default), encoding="utf-8")
        print(f"Wrote {args.output}")
    return 0


def _sports_cycle_with_defaults(args: argparse.Namespace) -> int:
    if args.output is None:
        args.output = str(default_sports_payload_path(args.run_id))
    return run_sports_cycle(args)


def _sports_export_features_with_defaults(args: argparse.Namespace) -> int:
    if args.output is None:
        args.output = str(default_sports_features_path(args.run_id))
    if args.labels_output is None:
        args.labels_output = str(default_sports_labels_path(args.run_id))
    return run_sports_export_features(args)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)
