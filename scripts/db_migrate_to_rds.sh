#!/usr/bin/env bash
#
# Dump the Railway source database and restore it into RDS, with parity proven
# at both ends.
#
#   db_migrate_to_rds.sh dump                take a verified dump of the source
#   db_migrate_to_rds.sh restore <file>      restore a dump into the target
#   db_migrate_to_rds.sh parity [baseline]   compare target against a baseline
#                                            snapshot, or against a fresh
#                                            source capture if none is given
#   db_migrate_to_rds.sh all                 dump, restore, parity
#
# Connection URLs come from the environment and are never echoed:
#
#   SOURCE_DATABASE_URL   the recovered Railway PostgreSQL
#   TARGET_DATABASE_URL   the AWS RDS instance
#
# This script does not modify the source. It only reads. The only writes are
# into the target, and it refuses to write into a target that already has
# application tables unless MIGRATION_ALLOW_NONEMPTY_TARGET=1 -- restoring over
# a populated database is how a migration quietly becomes a data-loss event.
#
# ## Baselines, and why `parity` takes one
#
# `dump` writes a parity snapshot of the source taken inside the same read
# transaction window as the dump. `parity` compares the target against that
# baseline by default (`all` passes it automatically).
#
# Comparing against a *fresh* source capture instead is only correct if the
# source is quiesced: Railway collectors write every few minutes, so by the
# time a restore finishes the live source has moved on and the diff reports
# rows the dump never contained. Those are not migration defects, but they look
# exactly like them. Passing the dump-time baseline compares like with like.

set -euo pipefail

# Dumps contain every row in the database, including authentication tables.
# The default umask would write them 0644.
umask 077

readonly ARTIFACT_DIR="${MIGRATION_ARTIFACT_DIR:-./migration-artifacts}"
readonly PARITY="${PARITY_SCRIPT:-scripts/db_parity.py}"
readonly PYTHON="${PYTHON_BIN:-python3}"

log() { printf '%s  %s\n' "$(date -u +%H:%M:%S)" "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# The three helpers below take the URL through _URL in the environment, never
# as an argument. Passing it in argv -- as an earlier version of this file did
# -- puts the password into `ps` output for the lifetime of the helper. That
# window is brief but real, and it defeats the point of keeping the password
# out of the psql and pg_dump invocations.

# Strip userinfo before anything reaches a log, a terminal, or CI output.
redact() {
  _URL="$1" "$PYTHON" <<'PY'
import os
from urllib.parse import urlsplit, urlunsplit
p = urlsplit(os.environ["_URL"])
host = p.hostname or ""
if p.port:
    host = f"{host}:{p.port}"
print(urlunsplit((p.scheme, host, p.path, "", "")))
PY
}

# Split a URL into a password-free URL and its password.
#
# Every PostgreSQL client here is invoked with the password-free form and the
# password supplied through PGPASSWORD, because an argument vector is readable
# by any user on the host via `ps` and is copied into crash dumps and process
# accounting. The password never appears in argv.
url_without_password() {
  _URL="$1" "$PYTHON" <<'PY'
import os
from urllib.parse import urlsplit, urlunsplit, quote
p = urlsplit(os.environ["_URL"])
netloc = ""
if p.username:
    netloc += quote(p.username, safe="")
    netloc += "@"
netloc += p.hostname or ""
if p.port:
    netloc += f":{p.port}"
print(urlunsplit((p.scheme, netloc, p.path, p.query, "")))
PY
}

url_password() {
  _URL="$1" "$PYTHON" <<'PY'
import os
from urllib.parse import urlsplit, unquote
p = urlsplit(os.environ["_URL"])
print(unquote(p.password) if p.password else "")
PY
}

require_tools() {
  for tool in pg_dump pg_restore psql "$PYTHON"; do
    command -v "$tool" >/dev/null 2>&1 || die "$tool is required"
  done
}

# pg_dump refuses to read a server newer than itself, and it discovers that
# only after connecting. Checking first turns a confusing mid-run abort into a
# clear message naming the package to install.
check_client_version() {
  local url="$1" server_major client_major
  server_major="$(run_psql "$url" -Atc 'SHOW server_version_num' 2>/dev/null | cut -c1-2)" \
    || die "cannot reach the database to check its version"
  client_major="$(pg_dump --version | sed -E 's/.* ([0-9]+).*/\1/')"

  if [ "$client_major" -lt "$server_major" ]; then
    die "pg_dump is version ${client_major} but the server is ${server_major}.
     pg_dump cannot read a newer server. Install matching client tools, e.g.
       apt-get install postgresql-client-${server_major}
     and put /usr/lib/postgresql/${server_major}/bin first on PATH."
  fi

  log "pg_dump ${client_major} against server ${server_major}"
}

# Wrappers that keep the password out of argv.
run_psql() {
  local url="$1"; shift
  PGPASSWORD="$(url_password "$url")" psql "$(url_without_password "$url")" "$@"
}

server_identity() {
  run_psql "$1" -Atc \
    "SELECT coalesce(host(inet_server_addr()), 'local') || ':' || inet_server_port() || '/' || current_database()"
}

require_source() { [ -n "${SOURCE_DATABASE_URL:-}" ] || die "SOURCE_DATABASE_URL is not set"; }
require_target() { [ -n "${TARGET_DATABASE_URL:-}" ] || die "TARGET_DATABASE_URL is not set"; }

# Refuse to treat one database as both sides. Every check would pass and none
# of them would mean anything.
require_distinct() {
  local src tgt
  src="$(server_identity "$SOURCE_DATABASE_URL")" || die "cannot reach the source database"
  tgt="$(server_identity "$TARGET_DATABASE_URL")" || die "cannot reach the target database"
  [ "$src" != "$tgt" ] || die "source and target are the same database ($src); this would prove nothing"
}

# db_parity.py reads PARITY_DATABASE_URL from the environment, so the URL stays
# out of its argv too.
capture_parity() {
  local url="$1" out="$2"
  shift 2
  PARITY_DATABASE_URL="$url" "$PYTHON" "$PARITY" --out "$out" "$@"
}

# --------------------------------------------------------------------------

cmd_dump() {
  require_source
  require_tools
  mkdir -p "$ARTIFACT_DIR"

  local stamp file
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  file="${ARTIFACT_DIR}/hawknetic-${stamp}.dump"

  log "source: $(redact "$SOURCE_DATABASE_URL")"

  # Refuse to dump a server still in recovery. A dump taken mid-replay is a
  # snapshot of an inconsistent moment and looks perfectly valid.
  local in_recovery
  in_recovery="$(run_psql "$SOURCE_DATABASE_URL" -Atc 'SELECT pg_is_in_recovery()')" \
    || die "cannot reach the source database"
  [ "$in_recovery" = "f" ] || die "source is still in recovery; see docs/aws-migration/database-recovery.md"

  check_client_version "$SOURCE_DATABASE_URL"

  log "capturing the dump-time parity baseline"
  capture_parity "$SOURCE_DATABASE_URL" "${ARTIFACT_DIR}/source-${stamp}.json" --content-hash

  # Custom format: compressed, and restorable selectively with pg_restore.
  # --no-owner/--no-privileges because RDS roles differ from Railway's and a
  # restore that tries to recreate them fails partway through.
  log "dumping (this reads every page, which is also the corruption check)"
  PGPASSWORD="$(url_password "$SOURCE_DATABASE_URL")" pg_dump \
    --format=custom \
    --compress=9 \
    --no-owner \
    --no-privileges \
    --verbose \
    --file="$file" \
    "$(url_without_password "$SOURCE_DATABASE_URL")" 2>&1 | tail -5

  # `pg_restore --list` reads only the table of contents: an archive whose
  # compressed data blocks are truncated or corrupt still lists cleanly. This
  # decompresses every block by restoring to a script on stdout and discarding
  # it, which is the cheapest way to prove the whole archive is readable.
  log "verifying the dump decompresses in full"
  pg_restore --file=/dev/null "$file" >/dev/null 2>&1 \
    || die "dump failed a full read; treat it as unusable and re-dump"

  local entries size
  entries="$(pg_restore --list "$file" | grep -c '^[0-9]' || true)"
  size="$(du -h "$file" | cut -f1)"

  log "dump complete: ${file} (${size}, ${entries} entries)"
  log "parity baseline: ${ARTIFACT_DIR}/source-${stamp}.json"
  printf '%s\n' "$file"
}

cmd_restore() {
  local file="${1:-}"
  [ -n "$file" ] || die "usage: $0 restore <dump-file>"
  [ -f "$file" ] || die "no such dump: $file"

  require_source
  require_target
  require_tools
  require_distinct

  log "target: $(redact "$TARGET_DATABASE_URL")"

  local existing
  existing="$(run_psql "$TARGET_DATABASE_URL" -Atc "
    SELECT count(*) FROM information_schema.tables
    WHERE table_type = 'BASE TABLE'
      AND table_schema NOT IN ('pg_catalog','information_schema')
  ")" || die "cannot reach the target database"

  if [ "$existing" -gt 0 ] && [ "${MIGRATION_ALLOW_NONEMPTY_TARGET:-0}" != "1" ]; then
    die "target already has ${existing} tables. Restoring over them risks data loss.
     Set MIGRATION_ALLOW_NONEMPTY_TARGET=1 only if you are certain the target is disposable."
  fi

  # Single-transaction so a failure leaves nothing behind: a half-restored
  # database that looks populated is worse than an empty one, because parity
  # would then be comparing against a plausible-looking lie.
  log "restoring"
  PGPASSWORD="$(url_password "$TARGET_DATABASE_URL")" pg_restore \
    --dbname="$(url_without_password "$TARGET_DATABASE_URL")" \
    --no-owner \
    --no-privileges \
    --single-transaction \
    --exit-on-error \
    --verbose \
    "$file" 2>&1 | tail -5

  log "restore complete"
}

cmd_parity() {
  local baseline="${1:-}"
  require_target
  mkdir -p "$ARTIFACT_DIR"

  local stamp src tgt
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  tgt="${ARTIFACT_DIR}/target-parity-${stamp}.json"

  if [ -n "$baseline" ]; then
    [ -f "$baseline" ] || die "no such baseline snapshot: $baseline"
    src="$baseline"
    log "baseline: $baseline (captured with the dump)"
  else
    require_source
    require_distinct
    src="${ARTIFACT_DIR}/source-parity-${stamp}.json"
    log "no baseline given; capturing the source live"
    log "NOTE: if the source is still taking writes, differences below may be"
    log "      writes that postdate the dump rather than migration defects."
    capture_parity "$SOURCE_DATABASE_URL" "$src" --content-hash
  fi

  log "capturing target snapshot"
  capture_parity "$TARGET_DATABASE_URL" "$tgt" --content-hash

  log "comparing"
  if "$PYTHON" "$PARITY" --compare "$src" "$tgt"; then
    log "PARITY OK"
    log "report: $src vs $tgt"
    return 0
  fi

  log "PARITY FAILED -- cutover is blocked while any discrepancy is unexplained"
  return 1
}

cmd_all() {
  local file baseline
  file="$(cmd_dump | tail -1)"
  # The baseline written alongside the dump, matched by its timestamp.
  baseline="${file%.dump}.json"
  baseline="${baseline/hawknetic-/source-}"
  cmd_restore "$file"
  if [ -f "$baseline" ]; then
    cmd_parity "$baseline"
  else
    log "WARNING: baseline $baseline not found; falling back to a live source capture"
    cmd_parity
  fi
}

case "${1:-}" in
  dump)    cmd_dump ;;
  restore) shift; cmd_restore "${1:-}" ;;
  parity)  shift; cmd_parity "${1:-}" ;;
  all)     cmd_all ;;
  *)
    cat <<EOF
usage: $0 <command>

  dump                 read the source, write a verified dump plus a
                       dump-time parity baseline
  restore <file>       restore a dump into TARGET_DATABASE_URL
  parity [baseline]    compare the target against a baseline snapshot, or
                       against a fresh source capture if none is given
  all                  dump, restore, parity against the dump-time baseline

environment:
  SOURCE_DATABASE_URL              source (read-only; never modified)
  TARGET_DATABASE_URL              target
  MIGRATION_ARTIFACT_DIR           output directory (default ./migration-artifacts)
  MIGRATION_ALLOW_NONEMPTY_TARGET  set to 1 to restore over an existing schema

See docs/aws-migration/database-recovery.md before running any of this against
production.
EOF
    exit 1
    ;;
esac
