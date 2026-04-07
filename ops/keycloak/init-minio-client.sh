#!/bin/sh
set -eu

KEYCLOAK_URL="http://keycloak:8080"
REALM="${KEYCLOAK_REALM:-aegra}"
CLIENT_ID="${MINIO_OIDC_CLIENT_ID:-minio-console}"
CLIENT_SECRET="${MINIO_OIDC_CLIENT_SECRET:-minio-console-local-secret}"
REDIRECT_URI="${MINIO_OIDC_REDIRECT_URI:-http://localhost:9001/oauth_callback}"
WEB_ORIGIN="${MINIO_OIDC_WEB_ORIGIN:-http://localhost:9001}"

until /opt/keycloak/bin/kcadm.sh config credentials --server "$KEYCLOAK_URL" --realm master --user "$KEYCLOAK_ADMIN_USER" --password "$KEYCLOAK_ADMIN_PASSWORD" >/dev/null 2>&1; do
  sleep 2
done

if /opt/keycloak/bin/kcadm.sh get clients -r "$REALM" -q clientId="$CLIENT_ID" | grep -Eq '"clientId"\s*:\s*"'"$CLIENT_ID"'"'; then
  exit 0
fi

/opt/keycloak/bin/kcadm.sh create clients -r "$REALM" \
  -s clientId="$CLIENT_ID" \
  -s name="MinIO Console" \
  -s description="Confidential OIDC client for MinIO console login" \
  -s enabled=true \
  -s protocol=openid-connect \
  -s publicClient=false \
  -s clientAuthenticatorType=client-secret \
  -s secret="$CLIENT_SECRET" \
  -s standardFlowEnabled=true \
  -s directAccessGrantsEnabled=false \
  -s serviceAccountsEnabled=false \
  -s implicitFlowEnabled=false \
  -s 'redirectUris=["'"$REDIRECT_URI"'"]' \
  -s 'webOrigins=["'"$WEB_ORIGIN"'"]' >/dev/null
