#!/usr/bin/env bash
# Run the Postgres/TimescaleDB integration tests against local Docker containers.
#
# Usage: ./scripts/run_integration_tests.sh [extra pytest args]
# Example: ./scripts/run_integration_tests.sh -k compression
set -euo pipefail
cd "$(dirname "$0")/.."

COMPOSE="docker compose -f docker-compose.test.yml"

cleanup() { $COMPOSE down -v; }
trap cleanup EXIT

$COMPOSE up -d --wait

export KNX_TEST_TIMESCALE_DSN="postgresql://knx:knxtest@localhost:5433/knx"
export KNX_TEST_PG_DSN="postgresql://knx:knxtest@localhost:5434/knx"

python -m pytest -m integration tests/integration -v "$@"
