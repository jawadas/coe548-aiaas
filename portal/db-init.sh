#!/bin/sh
# Database bootstrap for the portal. Runs as the one-shot Compose service
# "portal-db-init" every time the stack comes up.
#
# Postgres only runs init scripts on an empty data directory, and the shared
# volume already holds the gateway's database, so the portal's role and
# database are created here instead. After a successful run:
#
#   * role "portal" exists, can log in, has no other privilege, and has the
#     password from PORTAL_DB_PASSWORD (.env is always the truth);
#   * database "portal" exists, is owned by that role, and nobody else
#     (superusers aside) may connect to it.
#
# Idempotent: a second run changes nothing. It never connects to, reads or
# alters the "litellm" database. It works from the "postgres" maintenance
# database and only ever names the "portal" role and the "portal" database.
#
# Settings (standard libpq variables, set by Compose):
#   PGHOST, PGPORT, PGUSER, PGPASSWORD   the Postgres superuser
#   PGDATABASE                           maintenance database (default: postgres)
#   PORTAL_DB_PASSWORD                   password for the "portal" role
#
# POSIX sh. Needs psql 15 or newer (\getenv); postgres:16-alpine has it.

set -eu
LC_ALL=C
export LC_ALL

ROLE=portal
DATABASE=portal

log() { printf 'portal-db-init: %s\n' "$*"; }

fail() {
	printf 'portal-db-init: ERROR: %s\n' "$*" >&2
	exit 1
}

: "${PGHOST:=postgres}"
: "${PGDATABASE:=postgres}"
: "${PGCONNECT_TIMEOUT:=5}"
: "${PGAPPNAME:=portal-db-init}"
export PGHOST PGDATABASE PGCONNECT_TIMEOUT PGAPPNAME

[ -n "${PGUSER:-}" ] || fail 'PGUSER is not set. It must name the Postgres superuser.'

if [ -z "${PORTAL_DB_PASSWORD:-}" ]; then
	fail 'PORTAL_DB_PASSWORD is empty. Set it in .env (scripts/deploy.sh generates one, or run: openssl rand -hex 24).'
fi

# The same password is embedded in the portal's connection URL
# (postgresql://portal:<password>@postgres:5432/portal), where characters such
# as @ / : # ? % would quietly change the meaning of the URL. Refuse them here,
# with a clear message, rather than let the portal fail to connect later.
case "$PORTAL_DB_PASSWORD" in
*[!A-Za-z0-9._~-]*)
	fail 'PORTAL_DB_PASSWORD may contain only letters, digits and . _ ~ - because it is embedded in a connection URL. Generate one with: openssl rand -hex 24'
	;;
esac

log "waiting for Postgres at ${PGHOST}:${PGPORT:-5432}"
attempt=0
until pg_isready --quiet --timeout=3; do
	attempt=$((attempt + 1))
	if [ "$attempt" -ge 30 ]; then
		fail "Postgres at ${PGHOST}:${PGPORT:-5432} did not accept connections in time."
	fi
	sleep 1
done

# The password reaches psql through the environment (\getenv), never through
# the command line, and reaches SQL as a quoted literal (:'password' and %L),
# never by string concatenation. Each SELECT ... \gexec below builds one
# statement and runs it, or builds none when there is nothing to do.
if ! psql --no-psqlrc --no-password --quiet --tuples-only --no-align \
	--set ON_ERROR_STOP=1 --set role="$ROLE" --set db="$DATABASE" <<'SQL'; then
\getenv password PORTAL_DB_PASSWORD

-- The statements below carry the password, so keep them out of the server
-- log whatever its logging settings are (this session only).
SET log_statement = 'none';
SET log_min_error_statement = 'panic';
SET log_min_duration_statement = -1;

-- 1. The role: created on the first run ...
SELECT format('portal-db-init: role "%s": %s', :'role',
              CASE WHEN EXISTS (SELECT FROM pg_roles WHERE rolname = :'role')
                   THEN 'exists, enforcing its password and attributes'
                   ELSE 'creating it' END);

SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'role', :'password')
 WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = :'role')
\gexec

-- ... and brought back to the expected shape on every run: the password from
-- .env, and no privilege beyond logging in.
SELECT format('ALTER ROLE %I WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE'
              ' NOREPLICATION NOBYPASSRLS PASSWORD %L', :'role', :'password')
\gexec

-- 2. The database, owned by the role. template0 keeps it independent of
--    whatever may have been added to template1.
SELECT format('portal-db-init: database "%s": %s', :'db',
              CASE WHEN EXISTS (SELECT FROM pg_database WHERE datname = :'db')
                   THEN 'exists, enforcing its owner and access'
                   ELSE 'creating it' END);

SELECT format('CREATE DATABASE %I OWNER %I TEMPLATE template0 ENCODING ''UTF8''',
              :'db', :'role')
 WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = :'db')
\gexec

SELECT format('ALTER DATABASE %I OWNER TO %I', :'db', :'role')
  FROM pg_database
 WHERE datname = :'db' AND pg_get_userbyid(datdba) <> :'role'
\gexec

-- 3. Only the owner (and superusers) may connect to it.
SELECT format('REVOKE ALL ON DATABASE %I FROM PUBLIC', :'db')
\gexec
SQL
	fail "could not set up role \"$ROLE\" and database \"$DATABASE\" (the psql error is above)."
fi

# Prove what the portal needs: it can log in over the network with that
# password and may create its tables.
if ! can_create=$(PGUSER="$ROLE" PGPASSWORD="$PORTAL_DB_PASSWORD" PGDATABASE="$DATABASE" \
	psql --no-psqlrc --no-password --quiet --tuples-only --no-align --set ON_ERROR_STOP=1 \
	--command "SELECT has_schema_privilege(current_user, 'public', 'CREATE')"); then
	fail "role \"$ROLE\" cannot log in to database \"$DATABASE\" with PORTAL_DB_PASSWORD (the psql error is above)."
fi
if [ "$can_create" != t ]; then
	fail "role \"$ROLE\" may not create tables in schema public of database \"$DATABASE\". As the superuser, in that database, run: GRANT ALL ON SCHEMA public TO $ROLE"
fi

log "verified: \"$ROLE\" can log in to \"$DATABASE\" and create its tables"
log 'done'
