# shellcheck shell=bash
# Prisma schema sync for backend startup. Sourced by scripts/start,
# so it inherits that script's `set -o errexit -o nounset -o pipefail`
# and the environment already exported by scripts/env.sh.
#
# It brings the database schema up to date, choosing HOW based on the
# environment and whether the project uses Prisma Migrate:
#
#   no prisma/schema.prisma           -> nothing (Prisma not in use)
#   development                       -> db push       (fast iteration)
#   production, no migrations         -> db push       (legacy / migrate-less apps)
#   production, with migrations       -> migrate deploy (history-driven)
#
# "with migrations" means prisma/migrations/ actually contains migration
# directories. An empty or leftover migrations/ dir counts as "no migrations"
# so it can't flip a working db-push app onto the migrate path and park it.
#
# On the migrate-deploy path any failure PARKS the process (sleep
# infinity) instead of exiting non-zero. scripts/start is the runit
# `backend` service command, so a non-zero exit would make runit
# restart us straight into a crash loop against a half-migrated
# database. Parking keeps the box up for debugging, never resets or
# deletes data, and never launches the server against a partially
# migrated schema. See ENG-1188.

# Nothing to do if the project doesn't use Prisma. Normally sourced by
# scripts/start (return), but fall back to exit if run directly.
if [[ ! -f "prisma/schema.prisma" ]]; then
  # shellcheck disable=SC2317  # reachable when executed rather than sourced
  return 0 2>/dev/null || exit 0
fi

# Print a loud, actionable banner, leave a marker the platform can see, then
# block forever. Never returns.
#
# The marker matters: a parked backend still looks "running" to runit and to
# the platform, so without it a park is a SILENT outage — strictly harder to
# notice than the crash loop this replaces. The file is the signal an operator
# or a host-side check can alert on.
prisma_park() {
  local marker_dir="${DATA_DIR:-/data}"
  echo "########################################################################"
  echo "# FATAL: the database schema could not be updated safely."
  echo "# The backend has been PARKED (not restarted) to avoid a crash loop"
  echo "# and to avoid serving requests against a half-migrated database."
  echo "#"
  echo "# Reason: $*"
  echo "#"
  echo "# Nothing was reset and no data was deleted."
  echo "# To investigate, open a shell in the backend and run:"
  echo "#   cd backend && bunx prisma migrate status"
  echo "# If a migration is stuck in a failed state, resolve it explicitly,"
  echo "# e.g.  bunx prisma migrate resolve --rolled-back <migration_name>"
  echo "# then restart the backend service."
  echo "########################################################################"
  if [[ -d "${marker_dir}" ]]; then
    {
      echo "parked_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
      echo "reason=$*"
    } > "${marker_dir}/.vibecode-backend-parked" 2>/dev/null || true
  fi
  exec sleep infinity
}

# A generate failure (engine download, transient network) would otherwise exit
# non-zero under errexit and let runit restart us — the exact crash loop this
# script exists to remove. Park instead.
echo "Generating Prisma client..."
if ! bunx prisma generate; then
  prisma_park "prisma generate failed (see the error above)"
fi

sync_with_db_push() {
  echo "Pushing schema to database (db push)..."
  bunx prisma db push --accept-data-loss
}

migrate_deploy_or_park() {
  echo "Applying Prisma migrations (migrate deploy)..."
  if ! bunx prisma migrate deploy; then
    prisma_park "prisma migrate deploy failed (see the error above)"
  fi
}

# True only when prisma/migrations/ holds at least one migration directory.
# A missing, empty, or lock-file-only dir is NOT Prisma Migrate usage, so it
# must not route an otherwise-working db-push app onto the migrate path.
has_migrations() {
  [[ -d "prisma/migrations" ]] || return 1
  local entry
  for entry in prisma/migrations/*/; do
    [[ -d "${entry}" ]] && return 0
  done
  return 1
}

# Set while a baseline is part-way through. A baseline is several independent
# `resolve --applied` calls; the first success already creates
# _prisma_migrations, so a later failure would leave the database looking
# "tracked" while its history is incomplete. On the next boot that would take
# the tracked fast path and try to APPLY migrations whose changes are already
# present, failing on duplicate tables/columns and parking every restart. The
# marker makes that state recognisable so the baseline is finished instead.
BASELINE_MARKER="${DATA_DIR:-/data}/.vibecode-baseline-in-progress"

# Migration names already recorded in _prisma_migrations (empty when the table
# or the file is absent). SQLite-only, like the other probes.
recorded_migration_names() {
  [[ -n "${DB_FILE:-}" && -f "${DB_FILE:-}" ]] || return 0
  sqlite3 "${DB_FILE}" "SELECT migration_name FROM _prisma_migrations;" 2>/dev/null || true
}

# Migration directory names currently on disk, one per line.
migration_names_on_disk() {
  local migration_dir
  for migration_dir in prisma/migrations/*/; do
    [[ -d "${migration_dir}" ]] || continue
    basename "${migration_dir}"
  done
}

# Read-only drift check: does the live database already contain everything the
# migrations in $1 (default prisma/migrations) produce? Sets DRIFT_RC to
# prisma's exit code — 0 = match, 2 = drift, anything else = indeterminate —
# and always returns 0 so callers keep control under errexit.
#
# The customer database is only ever read. The shadow database prisma needs to
# replay migrations into is a throwaway kept OUTSIDE the data directory: a
# process killed mid-diff cannot then leave pid-suffixed debris next to
# production.db, where nothing prunes it and it would consume the tenant's
# quota and ride along into snapshots.
migrations_drift_rc() {
  local migrations_dir="${1:-prisma/migrations}" shadow_db
  shadow_db="${TMPDIR:-/tmp}/vibecode-migrate-shadow.$$.db"
  rm -f "${shadow_db}" "${shadow_db}-journal" "${shadow_db}-wal" "${shadow_db}-shm"

  set +e
  bunx prisma migrate diff \
    --from-url "${DATABASE_URL}" \
    --to-migrations "${migrations_dir}" \
    --shadow-database-url "file:${shadow_db}" \
    --exit-code
  DRIFT_RC=$?
  set -e

  rm -f "${shadow_db}" "${shadow_db}-journal" "${shadow_db}-wal" "${shadow_db}-shm"
  return 0
}

# Copy just the migrations named in $1 into the throwaway directory $2, so a
# drift check can ask "does the live database match what THESE migrations
# produce?" — precisely the claim the baseline marker makes. Comparing against
# every migration on disk instead would read a legitimately-pending migration
# added after the marker was written as drift. Fails if a named migration is
# gone, since the claim cannot then be re-verified at all.
materialise_verified_migrations() {
  local names="$1" dir="$2" name
  mkdir -p "${dir}" || return 1
  if [[ -f "prisma/migrations/migration_lock.toml" ]]; then
    cp "prisma/migrations/migration_lock.toml" "${dir}/" || return 1
  fi
  while IFS= read -r name; do
    [[ -n "${name}" ]] || continue
    [[ -d "prisma/migrations/${name}" ]] || return 1
    cp -R "prisma/migrations/${name}" "${dir}/" || return 1
  done <<<"${names}"
  return 0
}

# Mark every not-yet-recorded migration as applied. Idempotent: already
# recorded migrations are skipped, so this is safe to re-run after an
# interrupted baseline.
#
# $1 is the set of migrations the drift check actually verified as already
# present in the live database. Only those may be baselined: a migration added
# AFTER that check has never been verified, so recording it applied without
# running its SQL would leave the database behind the history Prisma reports —
# the silent divergence this script exists to prevent. Anything outside the set
# is left for `migrate deploy` to apply normally.
baseline_migrations() {
  local verified="$1"
  local recorded migration_dir migration_name
  recorded="$(recorded_migration_names)"
  for migration_dir in prisma/migrations/*/; do
    [[ -d "${migration_dir}" ]] || continue
    migration_name="$(basename "${migration_dir}")"
    if ! grep -qxF "${migration_name}" <<<"${verified}"; then
      echo "  added after the drift check, leaving to migrate deploy: ${migration_name}"
      continue
    fi
    if grep -qxF "${migration_name}" <<<"${recorded}"; then
      echo "  already recorded, skipping ${migration_name}"
      continue
    fi
    echo "  resolve --applied ${migration_name}"
    if ! bunx prisma migrate resolve --applied "${migration_name}"; then
      prisma_park "failed to baseline migration ${migration_name} ('prisma migrate resolve --applied'); the migration history is incomplete. It will be completed automatically on the next start; if it keeps failing, finish it by hand with 'bunx prisma migrate resolve --applied <migration_name>' for each migration still listed as pending."
    fi
  done
}

# Development and migrate-less apps keep the historical db push behavior.
if [[ "${ENVIRONMENT:-development}" != "production" ]]; then
  sync_with_db_push
elif ! has_migrations; then
  echo "No Prisma Migrate migrations found; using db push (project does not use Prisma Migrate)."
  sync_with_db_push
else
  # Production + Prisma Migrate. Never db push here.
  #
  # A database first built by `db push` has no _prisma_migrations table,
  # so `migrate deploy` fails with P3005. Such a DB can be baselined
  # (mark existing migrations as already-applied) ONLY when its live
  # schema already matches the migration head; otherwise baselining
  # would silently skip real pending changes (e.g. a new required
  # column) and leave the schema diverged. We gate baselining on a
  # read-only drift check that never mutates the customer database.

  # env.sh sets DATABASE_URL in production; guard anyway so a missing
  # value parks (loud, recoverable) instead of aborting under nounset
  # into a runit restart loop.
  if [[ -z "${DATABASE_URL:-}" ]]; then
    prisma_park "DATABASE_URL is not set; cannot verify schema drift or apply migrations safely."
  fi

  # env.sh exports DATABASE_FILE alongside DATABASE_URL, but only inside its
  # production branch. Derive it from the URL when absent so the baseline path
  # stays reachable, and strip any ?query suffix.
  DB_FILE="${DATABASE_FILE:-}"
  if [[ -z "${DB_FILE}" && "${DATABASE_URL}" == file:* ]]; then
    DB_FILE="${DATABASE_URL#file:}"
    DB_FILE="${DB_FILE%%\?*}"
  fi

  # Earlier versions put the drift check's shadow database next to
  # production.db, where a process killed mid-check left pid-suffixed files
  # that nothing prunes. Sweep any such debris; current runs keep the shadow
  # database outside the data directory entirely.
  if [[ -n "${DB_FILE}" ]]; then
    rm -f "${DB_FILE}".migrate-shadow.* 2>/dev/null || true
  fi

  # Whether we actually managed to look inside the database. Without this the
  # "no tables found" case is indistinguishable from "never looked", and an
  # untracked POPULATED database would be misread as fresh — the sqlite3 probes
  # are SQLite-only, so any other provider lands here too.
  db_inspected=false
  has_migrations_table=false
  db_has_tables=false
  if [[ -n "${DB_FILE}" && -f "${DB_FILE}" ]]; then
    db_inspected=true
    if [[ -n "$(sqlite3 "${DB_FILE}" \
        "SELECT name FROM sqlite_master WHERE type='table' AND name='_prisma_migrations' LIMIT 1;" \
        2>/dev/null)" ]]; then
      has_migrations_table=true
    fi
    if [[ -n "$(sqlite3 "${DB_FILE}" \
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' LIMIT 1;" \
        2>/dev/null)" ]]; then
      db_has_tables=true
    fi
  fi

  if [[ "${has_migrations_table}" == "true" ]]; then
    # Already tracked by Prisma Migrate: apply pending migrations. Unless a
    # previous baseline was interrupted part-way — then the history is
    # incomplete and those migrations are already present in the schema, so
    # finish recording them before deploying rather than trying to apply them.
    if [[ -f "${BASELINE_MARKER}" ]]; then
      echo "Found an interrupted baseline; completing the migration history before deploying..."
      verified_migrations="$(cat "${BASELINE_MARKER}" 2>/dev/null || true)"
      if [[ -z "${verified_migrations}" ]]; then
        # The marker exists but names no migrations, so there is no record of
        # what the drift check verified. Baselining blind could mark unrun SQL
        # as applied; deploying blind could re-apply present objects. Stop.
        prisma_park "found an interrupted baseline marker (${BASELINE_MARKER}) that lists no migrations, so it is unknown which are already present. Run 'bunx prisma migrate status', mark the already-present migrations with 'bunx prisma migrate resolve --applied <name>', delete that marker file, then restart."
      fi

      # The marker records WHICH migrations were verified, but not that the
      # claim is still true. This database need not be the one the drift check
      # ran against: a restore swaps the live database file, which can leave a
      # surviving marker paired with an older snapshot. Baselining on that
      # stale claim would mark migrations applied without running them — the
      # silent divergence this script exists to prevent, reached through the
      # recovery path. So re-verify now, against exactly the migrations the
      # marker names (not everything on disk, or a legitimately-pending
      # migration added since would read as drift).
      verified_dir="${TMPDIR:-/tmp}/vibecode-verified-migrations.$$"
      rm -rf "${verified_dir}"
      if ! materialise_verified_migrations "${verified_migrations}" "${verified_dir}"; then
        rm -rf "${verified_dir}"
        prisma_park "the interrupted baseline marker (${BASELINE_MARKER}) names a migration that is no longer on disk, so its claim cannot be re-verified. Run 'bunx prisma migrate status', reconcile the history by hand, delete that marker file, then restart."
      fi
      migrations_drift_rc "${verified_dir}"
      rm -rf "${verified_dir}"
      if [[ "${DRIFT_RC}" -ne 0 ]]; then
        prisma_park "an interrupted baseline was found, but the live schema no longer matches the migrations it had verified (drift check exited ${DRIFT_RC}); the database may have been replaced since, e.g. by a restore. Refusing to record migrations as applied without running them. Run 'bunx prisma migrate status', reconcile the history by hand, delete ${BASELINE_MARKER}, then restart."
      fi

      baseline_migrations "${verified_migrations}"
      rm -f "${BASELINE_MARKER}"
    fi
    migrate_deploy_or_park
  elif [[ "${db_inspected}" != "true" ]]; then
    # We could not inspect the database (non-SQLite provider, or no readable
    # file). Baselining requires proof the live schema matches the migration
    # head, and we have none — so never baseline here. `migrate deploy` is
    # correct for a fresh or already-tracked database, and an untracked
    # populated one fails P3005 and parks. Conservative by construction.
    echo "Could not inspect the database directly (non-SQLite provider or no readable file);"
    echo "applying migrations without baselining."
    migrate_deploy_or_park
  elif [[ "${db_has_tables}" != "true" ]]; then
    # Fresh/empty production database: deploy initializes it cleanly.
    migrate_deploy_or_park
  else
    # Untracked, non-empty DB (built by db push) -> P3005 territory.
    echo "Existing database has no Prisma Migrate history; checking for schema drift before baselining..."

    # Baselining marks MIGRATIONS as applied, so the safety question is
    # "does the live DB match what the migrations PRODUCE?" — not "does it
    # match schema.prisma?". Those differ when the schema and the migration
    # history disagree (a hand-written migration, or a reverted schema.prisma):
    # there the schema comparison reports "no diff" and would baseline a
    # migration whose changes never ran — the silent divergence this script
    # exists to prevent. So diff against the migrations, replayed into a
    # throwaway shadow database. The shadow DB is a fresh temp file, never the
    # customer database. (Verified against prisma 6.19.3: matched head -> 0,
    # DB behind migrations -> 2, migrations ahead of schema -> 2.)
    migrations_drift_rc
    diff_rc="${DRIFT_RC}"

    if [[ "${diff_rc}" -eq 0 ]]; then
      # Live DB matches what the migrations produce. Still STRUCTURAL only:
      # it cannot see non-schema migration effects — data backfills, triggers,
      # grants, policies. Baselining marks those migrations applied without
      # running them, so on a db-push-built DB such effects are ASSUMED
      # already present and are NOT executed. Surfaced loudly below.
      echo "Live schema already matches the migration head; baselining migrations as applied."
      echo "WARNING: baselining records migrations as applied WITHOUT running them."
      echo "         Structural schema is verified to match, but any non-schema"
      echo "         migration effects (data backfills, triggers, grants) are assumed"
      echo "         already present in this database and are NOT executed."
      # Mark the baseline as in progress first: the calls below are not atomic,
      # and a failure part-way must be recognisable on the next boot.
      #
      # The marker is the ONLY thing that makes an interrupted baseline
      # recoverable, so a baseline must not begin without it — otherwise a
      # later resolve failure leaves partial history no future boot can
      # recognise, and the backend parks on every restart. Fail closed.
      # The marker records exactly which migrations this drift check verified,
      # so a resume can never baseline one that was added afterwards.
      mkdir -p "$(dirname "${BASELINE_MARKER}")" 2>/dev/null || true
      if ! migration_names_on_disk > "${BASELINE_MARKER}" 2>/dev/null; then
        prisma_park "cannot create ${BASELINE_MARKER}; refusing to start a baseline that could not be resumed if it were interrupted. Make ${DATA_DIR:-/data} writable, then restart the backend service."
      fi
      # Read the set back rather than enumerating a second time, so the marker
      # is the single authority on what gets baselined and the record cannot
      # disagree with the action.
      baseline_migrations "$(cat "${BASELINE_MARKER}")"
      rm -f "${BASELINE_MARKER}"
      migrate_deploy_or_park
    elif [[ "${diff_rc}" -eq 2 ]]; then
      prisma_park "database predates Prisma Migrate and its live schema does not match the migration history (drift detected). Baselining would skip pending schema changes and risk data loss."
    else
      prisma_park "could not determine schema drift ('prisma migrate diff' exited ${diff_rc}); refusing to baseline blindly."
    fi
  fi
fi

# Enable DB viewer (idempotent, safe to call multiple times).
if [[ -n "${VIBECODE_PROJECT_ID:-}" ]]; then
  echo "Enabling database viewer..."
  curl -s -X POST "https://api.vibecodeapp.com/api/projects/${VIBECODE_PROJECT_ID}/cloud/db/enable" || true
fi
