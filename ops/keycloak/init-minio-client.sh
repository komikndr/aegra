#!/bin/sh
set -eu

KEYCLOAK_URL="http://keycloak:8080"
REALM="${KEYCLOAK_REALM:-aegra}"
API_CLIENT_ID="${KEYCLOAK_CLIENT_ID:-aegra-api}"
API_CLIENT_SECRET="${KEYCLOAK_CLIENT_SECRET:-}"
NEXTAUTH_CLIENT_ID="${KEYCLOAK_NEXTAUTH_CLIENT_ID:-executive-nextauth}"
NEXTAUTH_CLIENT_SECRET="${KEYCLOAK_NEXTAUTH_CLIENT_SECRET:-${KEYCLOAK_CLIENT_SECRET:-}}"
NEXTAUTH_REDIRECT_URI="${KEYCLOAK_NEXTAUTH_REDIRECT_URI:-http://localhost:3000/api/auth/callback/keycloak}"
NEXTAUTH_WEB_ORIGIN="${KEYCLOAK_NEXTAUTH_WEB_ORIGIN:-http://localhost:3000}"
CLIENT_ID="${MINIO_OIDC_CLIENT_ID:-minio-console}"
CLIENT_SECRET="${MINIO_OIDC_CLIENT_SECRET:-minio-console-local-secret}"
REDIRECT_URI="${MINIO_OIDC_REDIRECT_URI:-http://localhost:9001/oauth_callback}"
WEB_ORIGIN="${MINIO_OIDC_WEB_ORIGIN:-http://localhost:9001}"

get_client_uuid() {
  /opt/keycloak/bin/kcadm.sh get clients -r "$REALM" -q clientId="$1" --fields id,clientId \
    | sed -n 's/.*"id" : "\([^"]*\)".*/\1/p' \
    | head -n 1
}

update_client_secret() {
  client_id="$1"
  client_secret="$2"

  if [ -z "$client_secret" ]; then
    return
  fi

  client_uuid="$(get_client_uuid "$client_id")"
  if [ -z "$client_uuid" ]; then
    echo "Client '$client_id' not found in realm '$REALM'" >&2
    exit 1
  fi

  /opt/keycloak/bin/kcadm.sh update "clients/$client_uuid" -r "$REALM" \
    -s secret="$client_secret" >/dev/null
}

upsert_confidential_client() {
  client_id="$1"
  client_name="$2"
  description="$3"
  client_secret="$4"
  redirect_uri="$5"
  web_origin="$6"
  client_uuid="$(get_client_uuid "$client_id")"

  if [ -n "$client_uuid" ]; then
    /opt/keycloak/bin/kcadm.sh update "clients/$client_uuid" -r "$REALM" \
      -s clientId="$client_id" \
      -s name="$client_name" \
      -s description="$description" \
      -s enabled=true \
      -s protocol=openid-connect \
      -s publicClient=false \
      -s clientAuthenticatorType=client-secret \
      -s secret="$client_secret" \
      -s standardFlowEnabled=true \
      -s directAccessGrantsEnabled=false \
      -s serviceAccountsEnabled=false \
      -s implicitFlowEnabled=false \
      -s 'redirectUris=["'"$redirect_uri"'"]' \
      -s 'webOrigins=["'"$web_origin"'"]' >/dev/null
    return
  fi

  /opt/keycloak/bin/kcadm.sh create clients -r "$REALM" \
    -s clientId="$client_id" \
    -s name="$client_name" \
    -s description="$description" \
    -s enabled=true \
    -s protocol=openid-connect \
    -s publicClient=false \
    -s clientAuthenticatorType=client-secret \
    -s secret="$client_secret" \
    -s standardFlowEnabled=true \
    -s directAccessGrantsEnabled=false \
    -s serviceAccountsEnabled=false \
    -s implicitFlowEnabled=false \
    -s 'redirectUris=["'"$redirect_uri"'"]' \
    -s 'webOrigins=["'"$web_origin"'"]' >/dev/null
}

until /opt/keycloak/bin/kcadm.sh config credentials --server "$KEYCLOAK_URL" --realm master --user "$KEYCLOAK_ADMIN_USER" --password "$KEYCLOAK_ADMIN_PASSWORD" >/dev/null 2>&1; do
  sleep 2
done

update_client_secret "$API_CLIENT_ID" "$API_CLIENT_SECRET"

if [ -n "$NEXTAUTH_CLIENT_SECRET" ]; then
  upsert_confidential_client \
    "$NEXTAUTH_CLIENT_ID" \
    "Executive NextAuth" \
    "Confidential OIDC client for NextAuth server-side auth" \
    "$NEXTAUTH_CLIENT_SECRET" \
    "$NEXTAUTH_REDIRECT_URI" \
    "$NEXTAUTH_WEB_ORIGIN"
fi

upsert_confidential_client \
  "$CLIENT_ID" \
  "MinIO Console" \
  "Confidential OIDC client for MinIO console login" \
  "$CLIENT_SECRET" \
  "$REDIRECT_URI" \
  "$WEB_ORIGIN"
