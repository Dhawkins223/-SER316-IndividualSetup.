#!/usr/bin/env bash
#
# Dump the Railway source database and restore it into RDS, with parity proven
# at both ends.
#
#   db_migrate_to_rds.sh dump              take a verified dump of the source
#   db_migrate_to_rds.sh restore <file>    restore a dump into the target
#   db_migrate_to_rds.sh parity <file>     compare source and target snapshots
#   db_migrate_to_rds.sh all               dump, restore, parity
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

set -euo pipefail

readonly ARTIFACT_DIR="${MIGRATION_ARTIFACT_DIR:-./migration-artifacts}"
readonly PARITY="${PARITY_SCRIPT:-scripts/db_parity.py}"
readonly PYTHON="${PYTHON_BIN:-python3}"

log()  { printf '%s  %s\n' "$(date -u +%H:%M:%S)" "$*"; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# Strip userinfo before anything reaches a log, a terminal, or CI output.
redact() {
  "$PYTHON" - "$1" <<'PY'
import sys
from urllib.parse import urlsplit, urlunsplit
p = urlsplit(sys.argv[1])
host = p.hostname or ""
if p.port:
    host = f"{host}:{p.port}"
print(urlunsplit((p.scheme, host, p.path, "", "")))
PY
}

require_tools() {
  for tool in pg_dump pg_restore psql "$PYTHON"; do
    command -v "$tool" >/dev/null 2>&1 || die "$tool is required"
  done
}

# pg_dump refuses to read a server newer than itself, and it discovers that
# only after connecting. Checking first turns a confusing mid-run abort into a
# clear message naming the package to install -- and avoids doing minutes of
# work before failing.
check_client_version() {
  local url="$1" server_major client_major
  server_major="$(psql "$url" -Atc 'SHOW server_version_num' 2>/dev/null | cut -c1-2)" \
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

require_source() {
  [ -n "${SOURCE_DATABASE_URL:-}" ] || die "SOURCE_DATABASE_URL is not set"
}

require_target() {
  [ -n "${TARGET_DATABASE_URL:-}" ] || die "TARGET_DATABASE_URL is not set"
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
  in_recovery="$(psql "$SOURCE_DATABASE_URL" -Atc 'SELECT pg_is_in_recovery()')" \
    || die "cannot reach the source database"
  [ "$in_recovery" = "f" ] || die "source is still in recovery; see docs/aws-migration/database-recovery.md"

  check_client_version "$SOURCE_DATABASE_URL"

  log "capturing pre-dump parity snapshot"
  "$PYTHON" "$PARITY" --source "$SOURCE_DATABASE_URL" --out "${ARTIFACT_DIR}/source-${stamp}.json"

  # Custom format: compressed, and restorable selectively with pg_restore.
  # --no-owner/--no-privileges because RDS roles differ from Railway's and a
  # restore that tries to recreate them fails partway through.
  log "dumping (this reads every page, which is also the corruption check)"
  pg_dump \
    --format=custom \
    --compress=9 \
    --no-owner \
    --no-privileges \
    --verbose \
    --file="$file" \
    "$SOURCE_DATABASE_URL" 2>&1 | tail -5

  log "verifying the dump is readable"
  pg_restore --list "$file" >/dev/null || die "dump is not readable by pg_restore"

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

  require_target
  require_tools

  log "target: $(redact "$TARGET_DATABASE_URL")"

  # Refuse to restore over a populated database unless told explicitly.
  local existing
  existing="$(psql "$TARGET_DATABASE_URL" -Atc "
    SELECT count(*) FROM information_schema.tables
    WHERE table_type = 'BASE TABLE'
      AND table_schema NOT IN ('pg_catalog','information_schema')
  ")" || die "cannot reach the target database"

  if [ "$existing" -gt 0 ] && [ "${MIGRATION_ALLOW_NONEMPTY_TARGET:-0}" != "1" ]; then
    die "target already has ${existing} tables. Restoring over them risks data loss.
     Set MIGRATION_ALLOW_NONEMPTY_TARGET=1 only if you are certain the target is disposable."
  fi

  # --no-owner/--no-privileges to match the dump. Single-transaction so a
  # failure leaves nothing behind: a half-restored database that looks
  # populated is worse than an empty one, because parity would then be
  # comparing against a plausible-looking lie.
  log "restoring"
  pg_restore \
    --dbname="$TARGET_DATABASE_URL" \
    --no-owner \
    --no-privileges \
    --single-transaction \
    --exit-on-error \
    --verbose \
    "$file" 2>&1 | tail -5

  log "restore complete"
}

cmd_parity() {
  require_source
  require_target
  mkdir -p "$ARTIFACT_DIR"

  local stamp src tgt
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  src="${ARTIFACT_DIR}/source-parity-${stamp}.json"
  tgt="${ARTIFACT_DIR}/target-parity-${stamp}.json"

  log "capturing source snapshot"
  "$PYTHON" "$PARITY" --source "$SOURCE_DATABASE_URL" --out "$src"

  log "capturing target snapshot"
  "$PYTHON" "$PARITY" --source "$TARGET_DATABASE_URL" --out "$tgt"

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
  local file
  file="$(cmd_dump | tail -1)"
  cmd_restore "$file"
  cmd_parity
}

case "${1:-}" in
  dump)    cmd_dump ;;
  restore) shift; cmd_restore "${1:-}" ;;
  parity)  cmd_parity ;;
  all)     cmd_all ;;
  *)
    cat <<EOF
usage: $0 <command>

  dump              read the source, write a verified dump plus a parity baseline
  restore <file>    restore a dump into TARGET_DATABASE_URL
  parity            snapshot both databases and compare
  all               dump, restore, parity

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
