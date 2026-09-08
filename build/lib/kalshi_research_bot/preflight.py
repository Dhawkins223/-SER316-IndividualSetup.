"""One command that answers "is this environment actually fit to run".

The readiness checklist in `docs/deployment-readiness-checklist.md` asks for
things nobody can confirm from a code review: that migrations are applied *here*,
that the research-only controls hold *here*, that somebody can actually log in
*here*. Each of those is already computable, and each lives somewhere different —
`database_startup_status` behind `/readyz`, `production_safety_status` behind the
dashboard, the auth posture spread across environment flags and a user table. So
the answer existed but nobody could get it in one look, and the checklist items
stayed unticked because ticking them meant running four things and knowing how to
read each.

The failure this is aimed at is specific and has already happened twice. Migration
`0013` sat merged-but-unapplied while every worker crash-looped on
`postgres_database_not_ready`, and `0014` very likely repeated it, because nothing
in production applies migrations and nothing asks whether they were applied. A
pending migration is invisible until a collector dies of it.

Design decisions worth keeping:

- **Read-only, always.** This reports; it never applies a migration, creates an
  account, or writes a flag. An operator has to be able to run it against
  production without thinking about it.
- **A check that cannot run is not a check that passed.** Anything unresolvable
  reports `unknown` and fails the run, because "we could not tell" and "it is
  fine" are the two states this file exists to keep apart.
- **Every failure carries its remedy.** A gate that says only "no" sends the
  reader back to the docs; the point is to skip that trip.
"""

from __future__ import annotations

import os
from typing import Any, Mapping

# Exit codes, so this is usable as a deployment gate in a script.
EXIT_OK = 0
EXIT_FAILED = 1

PASS = "pass"
FAIL = "fail"
WARN = "warn"
UNKNOWN = "unknown"

# Only `pass` and `warn` let a run succeed. `unknown` deliberately does not.
_BLOCKING = frozenset({FAIL, UNKNOWN})

_TRUE = frozenset({"1", "true", "yes", "on"})


def _flag(values: Mapping[str, str], name: str, default: bool) -> bool:
    return str(values.get(name, str(default))).strip().lower() in _TRUE


def _check(
    name: str, status: str, detail: str, *, remedy: str | None = None
) -> dict[str, Any]:
    return {"name": name, "status": status, "detail": detail, "remedy": remedy}


def _is_hosted(values: Mapping[str, str]) -> bool:
    return bool(
        values.get("RAILWAY_ENVIRONMENT")
        or values.get("RAILWAY_PROJECT_ID")
        or str(values.get("APP_ENV") or "").lower() in {"staging", "production"}
    )


def check_migrations(status: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Whether every migration in the tree is applied to *this* database.

    The one check that would have caught both incidents. A merged migration that
    was never applied leaves the schema behind the code that expects it.
    """

    if status is None:
        try:
            from .database import database_startup_status

            status = database_startup_status()
        except Exception as error:  # pragma: no cover - defensive
            return _check(
                "migrations",
                UNKNOWN,
                f"could not read migration state: {type(error).__name__}",
                remedy="check DATABASE_URL and that the database is reachable",
            )

    pending = list(status.get("pending_versions") or [])
    state = str(status.get("state") or "unknown")
    if pending:
        return _check(
            "migrations",
            FAIL,
            f"{len(pending)} migration(s) not applied: {', '.join(str(v) for v in pending)}",
            remedy="PYTHONPATH=src python -m kalshi_research_bot database-migrate",
        )
    if not status.get("ready"):
        return _check(
            "migrations",
            FAIL,
            f"database not ready (state: {state})",
            remedy="check DATABASE_URL, then run database-status for the reason",
        )
    return _check("migrations", PASS, "all migrations applied")


def check_safety_controls(status: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The research-only controls, which are a hard gate rather than a default."""

    if status is None:
        try:
            from .database import production_safety_status

            status = production_safety_status()
        except Exception as error:  # pragma: no cover - defensive
            return _check(
                "safety_controls",
                UNKNOWN,
                f"could not evaluate safety controls: {type(error).__name__}",
            )

    failed = list(status.get("failed_controls") or [])
    hosted = bool(status.get("hosted"))
    where = "hosted" if hosted else "local"
    if failed:
        return _check(
            "safety_controls",
            FAIL,
            f"{where}: {len(failed)} control(s) not set safely: {', '.join(failed)}",
            remedy="set each to its safe value; hosted deployments must set them explicitly",
        )
    return _check("safety_controls", PASS, f"{where}: all research-only controls hold")


def check_auth_configuration(values: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Whether the way in is the intended one.

    Hosted deployments have two ways to authenticate and they are not equivalent:
    per-user accounts with roles and audit, and a single-owner Basic-auth
    fallback kept for emergencies. Running hosted on the fallback alone means one
    shared credential, no roles, and no audit trail — worth surfacing plainly
    rather than leaving it to whoever reads the environment next.
    """

    values = os.environ if values is None else values
    hosted = _is_hosted(values)
    user_auth = _flag(values, "DASHBOARD_USER_AUTH_ENABLED", False)
    registration = _flag(values, "AUTH_REGISTRATION_ENABLED", False)
    # The flag alone is not a sign-in path. `paper_server.dashboard_auth_configured`
    # requires a non-empty password for the fallback and serves 503 without one, so
    # a gate that accepted the flag by itself would approve an environment where
    # nobody can sign in — the exact thing these two checks exist to catch.
    basic_flag = _flag(values, "DASHBOARD_BASIC_FALLBACK_ENABLED", True)
    basic_password = bool(str(values.get("DASHBOARD_AUTH_PASSWORD") or "").strip())
    basic_fallback = basic_flag and basic_password

    if hosted and not _flag(values, "DASHBOARD_REQUIRE_AUTH_WHEN_HOSTED", False):
        return _check(
            "auth_configuration",
            FAIL,
            "hosted deployment does not require authentication",
            remedy="set DASHBOARD_REQUIRE_AUTH_WHEN_HOSTED=true",
        )
    if not user_auth and not basic_fallback:
        detail = (
            "the Basic fallback is enabled but DASHBOARD_AUTH_PASSWORD is empty, "
            "so the dashboard will serve 503 — nobody can sign in"
            if basic_flag
            else "no authentication path is enabled — nobody can sign in"
        )
        return _check(
            "auth_configuration",
            FAIL,
            detail,
            remedy="set DASHBOARD_USER_AUTH_ENABLED=true and stage accounts, "
            "or set DASHBOARD_AUTH_PASSWORD for the fallback",
        )
    if hosted and registration:
        return _check(
            "auth_configuration",
            FAIL,
            "AUTH_REGISTRATION_ENABLED is on in a hosted environment",
            remedy="unset it; account creation is a deliberate local operator action",
        )
    if not user_auth:
        return _check(
            "auth_configuration",
            WARN,
            "running on the single-owner Basic fallback: one shared credential, no roles, no audit",
            remedy="stage per-user accounts, then set DASHBOARD_USER_AUTH_ENABLED=true",
        )
    if hosted and basic_fallback:
        return _check(
            "auth_configuration",
            WARN,
            "per-user accounts are enabled and the Basic fallback is still open",
            remedy="disable DASHBOARD_BASIC_FALLBACK_ENABLED once accounts are verified",
        )
    return _check("auth_configuration", PASS, "per-user accounts enabled, fallback closed")


def check_sign_in_possible(values: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Whether an account that can actually sign in exists.

    Configuration saying accounts are enabled and an account existing are
    different facts, and a deployment can satisfy the first while nobody can log
    in. Only counts accounts that are not disabled.
    """

    values = os.environ if values is None else values
    if not _flag(values, "DASHBOARD_USER_AUTH_ENABLED", False):
        return _check(
            "sign_in_possible",
            WARN,
            "per-user accounts are disabled; sign-in depends on the Basic fallback",
        )
    try:
        # Deliberately not `LocalAuthStore()`. Its constructor calls
        # `ensure_database_ready`, which *applies* every pending migration when
        # DATABASE_MIGRATION_MODE=apply — so the read-only gate would silently
        # mutate the schema it was invoked to inspect, outside any readiness
        # gate, and then report the pre-mutation state. Force check mode and
        # open the pool directly.
        from dataclasses import replace

        from .database import DatabaseSettings, connection_pool

        settings = replace(DatabaseSettings.from_env(), migration_mode="check")
        if not settings.database_url:
            return _check(
                "sign_in_possible",
                UNKNOWN,
                "no DATABASE_URL, so accounts cannot be counted",
                remedy="set DATABASE_URL to the environment being checked",
            )
        with connection_pool(settings).connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM auth.app_users WHERE is_disabled = FALSE"
            ).fetchone()
        total = int((row or {}).get("total") or 0) if isinstance(row, Mapping) else int(row[0])
    except Exception as error:
        return _check(
            "sign_in_possible",
            UNKNOWN,
            f"could not count active accounts: {type(error).__name__}",
            remedy="confirm the database is reachable and migrations are applied",
        )

    if total == 0:
        return _check(
            "sign_in_possible",
            FAIL,
            "per-user accounts are enabled but no active account exists",
            remedy="AUTH_REGISTRATION_ENABLED=true AUTH_NEW_USER_PASSWORD=... "
            "python -m kalshi_research_bot auth-create-user --username <name> --role admin",
        )
    return _check("sign_in_possible", PASS, f"{total} active account(s)")


def run_preflight(values: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Every gate, in one read-only pass."""

    values = os.environ if values is None else values
    checks = [
        check_migrations(),
        check_safety_controls(),
        check_auth_configuration(values),
        check_sign_in_possible(values),
    ]
    counts: dict[str, int] = {}
    for check in checks:
        counts[check["status"]] = counts.get(check["status"], 0) + 1
    blocking = [check for check in checks if check["status"] in _BLOCKING]
    return {
        "hosted": _is_hosted(values),
        "ready": not blocking,
        "checks": checks,
        "counts": counts,
        "blocking": [check["name"] for check in blocking],
    }


_SYMBOL = {PASS: "PASS", FAIL: "FAIL", WARN: "WARN", UNKNOWN: "????"}


def render_preflight(report: Mapping[str, Any]) -> str:
    """Plain text, aligned, so a failure is obvious in a deploy log."""

    where = "hosted" if report.get("hosted") else "local"
    lines = [f"Preflight ({where})", ""]
    width = max((len(str(c["name"])) for c in report.get("checks") or []), default=10)
    for check in report.get("checks") or []:
        lines.append(
            f"  [{_SYMBOL.get(str(check['status']), '????')}]  "
            f"{str(check['name']).ljust(width)}  {check['detail']}"
        )
        if check.get("remedy") and check["status"] in _BLOCKING:
            lines.append(f"          {' ' * width}  -> {check['remedy']}")
    lines.append("")
    if report.get("ready"):
        lines.append("Ready. No blocking gate failed.")
    else:
        blocking = ", ".join(str(name) for name in report.get("blocking") or [])
        lines.append(f"NOT READY. Blocking: {blocking}")
    return "\n".join(lines)
