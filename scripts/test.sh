#!/bin/sh
# Run all tests on a throw-away stack (project "memtest", other ports), so real memories are never touched.
set -e
cd "$(dirname "$0")/.."
export COMPOSE_PROJECT_NAME=memtest APP_PORT=8100 DB_PORT=5434 S3_PORT=9100
trap 'docker compose down -v >/dev/null 2>&1' EXIT

docker compose up -d >/dev/null 2>&1
for i in $(seq 1 60); do curl -s "localhost:$APP_PORT/health" | grep -q '"ok"' && break; sleep 2; done
docker compose exec -T -w /srv app python -m pytest -q -p no:cacheprovider -W ignore tests "$@"
