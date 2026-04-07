#!/bin/sh
set -eu

if [ "$#" -lt 2 ]; then
  echo "Usage: $0 <local|staging> <docker-compose args...>" >&2
  exit 1
fi

TARGET="$1"
shift

case "$TARGET" in
  local)
    ENV_FILE=".env"
    COMPOSE_FILE="docker-compose.yml"
    APP_ENV_FILE=".env"
    ;;
  staging)
    ENV_FILE="${STAGING_ENV_FILE:-/home/kxn/onyx_ai_secret/env.staging.backend}"
    COMPOSE_FILE="docker-compose-staging.yaml"
    APP_ENV_FILE="$ENV_FILE"
    ;;
  *)
    echo "Unknown target '$TARGET'. Use 'local' or 'staging'." >&2
    exit 1
    ;;
esac

if [ ! -f "$ENV_FILE" ]; then
  echo "Missing $ENV_FILE. Create it from ${ENV_FILE}.example or the appropriate template first." >&2
  exit 1
fi

export APP_ENV_FILE
exec docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"
