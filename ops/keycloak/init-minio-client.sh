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
MESSAGE_GATEWAY_CLIENT_ID="${MESSAGE_GATEWAY_KEYCLOAK_CLIENT_ID:-aegra-message-gateway}"

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

ensure_optional_client_scope() {
  client_uuid="$1"
  scope_name="$2"
  scope_uuid="$(
    /opt/keycloak/bin/kcadm.sh get client-scopes -r "$REALM" -q name="$scope_name" --fields id,name \
      | sed -n 's/.*"id" : "\([^"]*\)".*/\1/p' \
      | head -n 1
  )"

  if [ -z "$scope_uuid" ]; then
    echo "Client scope '$scope_name' not found in realm '$REALM'" >&2
    exit 1
  fi

  assigned="$({
    /opt/keycloak/bin/kcadm.sh get "clients/$client_uuid/optional-client-scopes" -r "$REALM" --fields name
  } | sed -n 's/.*"name" : "\([^"]*\)".*/\1/p' | grep -Fx "$scope_name" || true)"
  if [ -z "$assigned" ]; then
    /opt/keycloak/bin/kcadm.sh update "clients/$client_uuid/optional-client-scopes/$scope_uuid" -r "$REALM" >/dev/null
  fi
}

ensure_offline_access_for_users() {
  /opt/keycloak/bin/kcadm.sh get users -r "$REALM" -q max=1000 --fields username \
    | sed -n 's/.*"username" : "\([^"]*\)".*/\1/p' \
    | while IFS= read -r username; do
        case "$username" in
          service-account-*) continue ;;
        esac
        /opt/keycloak/bin/kcadm.sh add-roles -r "$REALM" --uusername "$username" --rolename offline_access \
          >/dev/null
      done
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

upsert_message_gateway_client() {
  client_id="$1"
  client_uuid="$(get_client_uuid "$client_id")"

  if [ -n "$client_uuid" ]; then
    /opt/keycloak/bin/kcadm.sh update "clients/$client_uuid" -r "$REALM" \
      -s clientId="$client_id" \
      -s name="Aegra Message Gateway" \
      -s description="Public device-authorization client for linked messaging identities" \
      -s enabled=true \
      -s protocol=openid-connect \
      -s publicClient=true \
      -s standardFlowEnabled=false \
      -s directAccessGrantsEnabled=false \
      -s serviceAccountsEnabled=false \
      -s implicitFlowEnabled=false \
      -s 'attributes={"oauth2.device.authorization.grant.enabled":"true"}' >/dev/null
  else
    /opt/keycloak/bin/kcadm.sh create clients -r "$REALM" \
      -s clientId="$client_id" \
      -s name="Aegra Message Gateway" \
      -s description="Public device-authorization client for linked messaging identities" \
      -s enabled=true \
      -s protocol=openid-connect \
      -s publicClient=true \
      -s standardFlowEnabled=false \
      -s directAccessGrantsEnabled=false \
      -s serviceAccountsEnabled=false \
      -s implicitFlowEnabled=false \
      -s 'attributes={"oauth2.device.authorization.grant.enabled":"true"}' >/dev/null
    client_uuid="$(get_client_uuid "$client_id")"
  fi

  mapper_exists="$(
    /opt/keycloak/bin/kcadm.sh get "clients/$client_uuid/protocol-mappers/models" -r "$REALM" --fields name \
      | sed -n 's/.*"name" : "\(audience-aegra-api\)".*/\1/p' \
      | head -n 1
  )"
  if [ -z "$mapper_exists" ]; then
    /opt/keycloak/bin/kcadm.sh create "clients/$client_uuid/protocol-mappers/models" -r "$REALM" \
      -s name=audience-aegra-api \
      -s protocol=openid-connect \
      -s protocolMapper=oidc-audience-mapper \
      -s consentRequired=false \
      -s 'config={"included.client.audience":"aegra-api","id.token.claim":"false","access.token.claim":"true"}' >/dev/null
  fi

  ensure_optional_client_scope "$client_uuid" offline_access
}

until /opt/keycloak/bin/kcadm.sh config credentials --server "$KEYCLOAK_URL" --realm master --user "$KEYCLOAK_ADMIN_USER" --password "$KEYCLOAK_ADMIN_PASSWORD" >/dev/null 2>&1; do
  sleep 2
done

update_client_secret "$API_CLIENT_ID" "$API_CLIENT_SECRET"
upsert_message_gateway_client "$MESSAGE_GATEWAY_CLIENT_ID"
ensure_offline_access_for_users

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
