from __future__ import annotations

import json
import signal
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Mapping

from .connectors.slack_alerts import build_alert_payload, send_alert
from .database import DatabaseSettings, close_connection_pools, database_startup_status
from .monitoring import WorkerMonitorStore


WorkerOperation = Callable[[], Mapping[str, Any]]
_CURRENT_IDEMPOTENCY_KEY: ContextVar[str | None] = ContextVar(
    "worker_idempotency_key",
    default=None,
)


class NonRetryableWorkerError(RuntimeError):
    pass


def start_worker_health_server(service_name: str, port: int) -> ThreadingHTTPServer:
    class WorkerHealthHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/healthz":
                self._send_json(200, {"status": "ok", "service": service_name})
                return
            if self.path == "/readyz":
                database = database_startup_status()
                ready = bool(database.get("ready"))
                self._send_json(
                    200 if ready else 503,
                    {
                        "status": "ready" if ready else "blocked",
                        "service": service_name,
                        "database": {
                            "backend": database.get("backend") or database.get("dialect"),
                            "state": database.get("state"),
                            "ready": ready,
                            "pending_versions": database.get("pending_versions", []),
                        },
                    },
                )
                return
            self.send_error(404)

        def _send_json(self, status_code: int, payload: Mapping[str, Any]) -> None:
            body = json.dumps(payload, sort_keys=True).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("0.0.0.0", int(port)), WorkerHealthHandler)
    thread = threading.Thread(
        target=server.serve_forever,
        name=f"{service_name}-health",
        daemon=True,
    )
    thread.start()
    return server


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(value: datetime | None = None) -> str:
    return (value or utc_now()).isoformat()


def cadence_idempotency_key(worker_name: str, cadence_seconds: int, *, now: datetime | None = None) -> str:
    timestamp = int((now or utc_now()).timestamp())
    bucket = timestamp // max(1, int(cadence_seconds))
    return f"{worker_name}:{cadence_seconds}:{bucket}"


def current_worker_idempotency_key() -> str | None:
    return _CURRENT_IDEMPOTENCY_KEY.get()


def structured_worker_log(event: Mapping[str, Any], *, writer: Callable[[str], Any] = print) -> None:
    safe = {
        key: value
        for key, value in event.items()
        if not any(secret in str(key).lower() for secret in ("password", "secret", "token", "api_key", "webhook"))
    }
    writer(json.dumps(safe, sort_keys=True, default=str))


@dataclass(frozen=True)
class WorkerSpec:
    name: str
    asset_class: str
    cadence_seconds: int
    maximum_attempts: int = 3
    initial_backoff_seconds: float = 2.0
    expect_records: bool = False
    alert_after_failures: int = 3


def run_worker_once(
    spec: WorkerSpec,
    operation: WorkerOperation,
    *,
    run_id: str,
    settings: DatabaseSettings | None = None,
    idempotency_key: str | None = None,
    now: datetime | None = None,
    sleep: Callable[[float], Any] = time.sleep,
    log_writer: Callable[[str], Any] = print,
) -> dict[str, Any]:
    attempted = now or utc_now()
    key = idempotency_key or cadence_idempotency_key(spec.name, spec.cadence_seconds, now=attempted)
    monitor = WorkerMonitorStore(settings)
    if not monitor.start_run(
        worker_name=spec.name,
        asset_class=spec.asset_class,
        run_id=run_id,
        idempotency_key=key,
        attempted_at=utc_iso(attempted),
    ):
        result = {
            "status": "skipped_duplicate",
            "worker_name": spec.name,
            "run_id": run_id,
            "idempotency_key": key,
        }
        structured_worker_log(result, writer=log_writer)
        return result
    structured_worker_log(
        {"event": "worker_started", "worker_name": spec.name, "run_id": run_id, "idempotency_key": key},
        writer=log_writer,
    )
    final_error: Exception | None = None
    attempts_made = 0
    for attempt in range(1, max(1, spec.maximum_attempts) + 1):
        attempts_made = attempt
        try:
            token = _CURRENT_IDEMPOTENCY_KEY.set(key)
            try:
                raw_result = dict(operation())
            finally:
                _CURRENT_IDEMPOTENCY_KEY.reset(token)
            records_processed = int(raw_result.get("records_processed") or 0)
            no_material_change = bool(raw_result.get("no_material_change"))
            if spec.expect_records and records_processed == 0 and not no_material_change:
                raise RuntimeError("unexpected_zero_records")
            finished_at = utc_iso()
            monitor.finish_success(
                worker_name=spec.name,
                idempotency_key=key,
                finished_at=finished_at,
                records_processed=records_processed,
                details=raw_result,
                data_fresh_at=raw_result.get("data_fresh_at"),
                source_fresh_at=raw_result.get("source_fresh_at"),
                pending_settlements=int(raw_result.get("pending_settlements") or 0),
                model_state=raw_result.get("model_state"),
            )
            result = {
                "status": "success",
                "worker_name": spec.name,
                "run_id": run_id,
                "idempotency_key": key,
                "attempts": attempt,
                **raw_result,
            }
            structured_worker_log({"event": "worker_succeeded", **result}, writer=log_writer)
            return result
        except Exception as exc:
            final_error = exc
            structured_worker_log(
                {
                    "event": "worker_attempt_failed",
                    "worker_name": spec.name,
                    "run_id": run_id,
                    "attempt": attempt,
                    "error_code": f"{type(exc).__name__}:{str(exc)[:120]}",
                },
                writer=log_writer,
            )
            if isinstance(exc, NonRetryableWorkerError):
                break
            if attempt < max(1, spec.maximum_attempts):
                sleep(spec.initial_backoff_seconds * (2 ** (attempt - 1)))
    error_code = f"{type(final_error).__name__}:{str(final_error)[:120]}" if final_error else "worker_failed"
    consecutive_failures = monitor.finish_failure(
        worker_name=spec.name,
        idempotency_key=key,
        finished_at=utc_iso(),
        error_code=error_code,
        details={"attempts": attempts_made, "error_code": error_code},
    )
    alert_status = {"status": "not_applicable"}
    if consecutive_failures >= spec.alert_after_failures:
        alert_status = send_alert(
            build_alert_payload(
                bot_name=spec.name,
                asset_class=spec.asset_class,
                run_id=run_id,
                severity="critical" if consecutive_failures >= 5 else "warning",
                event_type="consecutive_worker_failures",
                message=f"{spec.name} failed {consecutive_failures} consecutive runs",
                next_action="inspect private worker status and source/database health",
            )
        )
    result = {
        "status": "failed",
        "worker_name": spec.name,
        "run_id": run_id,
        "idempotency_key": key,
        "attempts": attempts_made,
        "error_code": error_code,
        "consecutive_failures": consecutive_failures,
        "alert_status": alert_status.get("status"),
    }
    structured_worker_log({"event": "worker_failed", **result}, writer=log_writer)
    return result


CYCLE_CRASH_POOL_RESET_THRESHOLD = 3
# Bounds the exponent only, so a long outage cannot build an absurd integer. The
# delay itself is clamped by the worker's cadence well before this is reached.
CYCLE_CRASH_MAX_BACKOFF_STEPS = 20


def run_worker_forever(
    spec: WorkerSpec,
    operation: WorkerOperation,
    *,
    run_id: str,
    settings: DatabaseSettings | None = None,
    stop_event: threading.Event | None = None,
    log_writer: Callable[[str], Any] = print,
) -> int:
    stopping = stop_event or threading.Event()

    def request_stop(_signum=None, _frame=None):
        stopping.set()

    for signal_name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, signal_name):
            try:
                signal.signal(getattr(signal, signal_name), request_stop)
            except ValueError:
                pass
    consecutive_crashes = 0
    while not stopping.is_set():
        # run_worker_once records its own operation failures, but its bookkeeping
        # calls sit outside that retry loop: a Postgres connection dropped during
        # start_run or finish_failure escapes here. Letting it end the process
        # turns a transient blip into a permanently stopped collector, so the loop
        # absorbs it and tries again on the next tick.
        try:
            run_worker_once(spec, operation, run_id=run_id, settings=settings)
            consecutive_crashes = 0
            delay = float(spec.cadence_seconds)
        except Exception as exc:  # noqa: BLE001 - a crashed cycle must not stop the worker
            consecutive_crashes += 1
            structured_worker_log(
                {
                    "event": "worker_cycle_crashed",
                    "worker_name": spec.name,
                    "run_id": run_id,
                    "error_code": f"{type(exc).__name__}:{str(exc)[:120]}",
                    "consecutive_crashes": consecutive_crashes,
                },
                writer=log_writer,
            )
            if consecutive_crashes >= CYCLE_CRASH_POOL_RESET_THRESHOLD:
                # A pool holding broken connections keeps failing the same way.
                # Drop it so the next cycle dials fresh ones.
                close_connection_pools()
            # Back off geometrically until the wait reaches the worker's own
            # cadence, then hold there. A database that stays down must not be
            # retried faster than the worker would have run anyway -- an earlier
            # version capped the exponent at four steps, which pinned an hourly
            # worker to a 16-second retry and produced over a thousand crash
            # cycles against a database that was in recovery.
            steps = min(consecutive_crashes - 1, CYCLE_CRASH_MAX_BACKOFF_STEPS)
            delay = min(
                float(spec.cadence_seconds),
                spec.initial_backoff_seconds * (2**steps),
            )
        stopping.wait(delay)
    structured_worker_log(
        {"event": "worker_stopped", "worker_name": spec.name, "run_id": run_id},
        writer=log_writer,
    )
    return 0
