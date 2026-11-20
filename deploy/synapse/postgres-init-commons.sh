#!/bin/sh
# Runs once when the PostgreSQL volume is first created (docker-entrypoint-initdb.d).
# COMMONS gets its own database and role. It cannot connect to the Synapse or
# MAS databases: the app talks to Matrix only through its APIs.
set -eu
password=$(cat /run/secrets/database_password)
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
	--set=pw="$password" <<'SQL'
CREATE ROLE commons LOGIN PASSWORD :'pw';
CREATE DATABASE commons OWNER commons;
REVOKE CONNECT ON DATABASE synapse FROM PUBLIC;
REVOKE CONNECT ON DATABASE mas FROM PUBLIC;
REVOKE CONNECT ON DATABASE commons FROM PUBLIC;
GRANT CONNECT ON DATABASE commons TO commons;
SQL
