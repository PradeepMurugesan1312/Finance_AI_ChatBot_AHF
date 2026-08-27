#!/usr/bin/env bash
# ============================================================================
# create-destination.sh
#
# Creates / updates the BTP HTTP destination that connects SAP Joule to this
# CF-deployed A2A agent. Adapted from the sibling ap_inquiry_agent's proven
# script — same logic, this project's defaults.
#
# Prerequisites:
#   - cf CLI v8, logged in and targeting the right org/space
#   - python3 (JSON parsing — jq breaks on PEM keys in service-key output)
#   - the agent app already deployed (cf push)
#   - a `destination` service instance in the subaccount
#
# Usage:
#   ./create-destination.sh \
#     --agent-name finance-ai-chatbot \
#     --destination-name AHF_FINANCE_AGENT_DEV \
#     --landscape us10-001
# ============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
if [[ -f "${PROJECT_ROOT}/.env" ]]; then
  echo "Loading .env from ${PROJECT_ROOT}/.env"
  set -a; source "${PROJECT_ROOT}/.env"; set +a
fi

# ---------- defaults (override via .env or CLI args) ----------
AGENT_NAME="${CF_APP_NAME:-finance-ai-chatbot}"
DESTINATION_NAME="${DESTINATION_NAME:-AHF_FINANCE_AGENT_DEV}"
LANDSCAPE="${CF_LANDSCAPE:-us10-001}"
DEST_SERVICE_INSTANCE="${DEST_SERVICE_INSTANCE:-destination-service}"
DEST_SERVICE_KEY="${DEST_SERVICE_KEY:-destination-service-key}"
# NoAuthentication is a tracked dev gap. Production: OAuth2SAMLBearerAssertion /
# IAS App2App, configured by an SAP admin (see joule-capability/README.md).
AUTH_TYPE="${AUTH_TYPE:-NoAuthentication}"
AGENT_URL=""

usage() {
  cat <<EOF
Usage: $(basename "$0") [OPTIONS]

  --agent-name NAME          CF app name            (default: ${AGENT_NAME})
  --destination-name NAME    BTP destination name   (default: ${DESTINATION_NAME})
  --landscape LANDSCAPE      CF landscape           (default: ${LANDSCAPE})
  --agent-url URL            Override agent URL (auto-detected from cf app if omitted)
  --dest-service-instance    Destination service instance (default: ${DEST_SERVICE_INSTANCE})
  --dest-service-key         Destination service key      (default: ${DEST_SERVICE_KEY})
  --auth TYPE                NoAuthentication (default) | OAuth2ClientCredentials
EOF
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --agent-name)            AGENT_NAME="$2";             shift 2 ;;
    --destination-name)      DESTINATION_NAME="$2";       shift 2 ;;
    --landscape)             LANDSCAPE="$2";              shift 2 ;;
    --agent-url)             AGENT_URL="$2";              shift 2 ;;
    --dest-service-instance) DEST_SERVICE_INSTANCE="$2";  shift 2 ;;
    --dest-service-key)      DEST_SERVICE_KEY="$2";       shift 2 ;;
    --auth)                  AUTH_TYPE="$2";              shift 2 ;;
    -h|--help)               usage ;;
    *) echo "Unknown option: $1"; usage ;;
  esac
done

for cmd in cf curl python3; do
  command -v "$cmd" &>/dev/null || { echo "Error: '$cmd' is required."; exit 1; }
done

echo "=== BTP destination: ${DESTINATION_NAME} -> agent ${AGENT_NAME} ==="

# ---- 1. resolve the agent URL from the CF route ----
if [[ -z "$AGENT_URL" ]]; then
  echo "[1/5] Detecting route for '${AGENT_NAME}'..."
  GUID=$(cf app "$AGENT_NAME" --guid 2>/dev/null) || {
    echo "Error: CF app '${AGENT_NAME}' not found. Run 'cf push' first."; exit 1; }
  ROUTE=$(cf curl "/v3/apps/${GUID}/routes" 2>/dev/null \
    | python3 -c "import json,sys; r=json.load(sys.stdin).get('resources',[]); print(r[0]['url'] if r else '')")
  if [[ -z "$ROUTE" ]]; then
    AGENT_URL="https://${AGENT_NAME}.cfapps.${LANDSCAPE}.hana.ondemand.com"
    echo "  No route via API; defaulting to ${AGENT_URL}"
  else
    AGENT_URL="https://${ROUTE}"; echo "  ${AGENT_URL}"
  fi
else
  echo "[1/5] Using provided agent URL: ${AGENT_URL}"
fi

echo "  Probing ${AGENT_URL}/.well-known/agent-card.json ..."
CODE=$(curl -s -o /dev/null -w "%{http_code}" "${AGENT_URL}/.well-known/agent-card.json" || echo 000)
[[ "$CODE" == "200" ]] && echo "  agent card OK" || echo "  warning: agent card returned HTTP ${CODE} (continuing)"

# ---- 2. destination service instance ----
echo "[2/5] Destination service instance '${DEST_SERVICE_INSTANCE}'..."
if cf service "$DEST_SERVICE_INSTANCE" &>/dev/null; then
  echo "  exists"
else
  cf create-service destination lite "$DEST_SERVICE_INSTANCE"
  sleep 5
fi

# ---- 3. service key ----
echo "[3/5] Service key '${DEST_SERVICE_KEY}'..."
cf service-key "$DEST_SERVICE_INSTANCE" "$DEST_SERVICE_KEY" &>/dev/null \
  || cf create-service-key "$DEST_SERVICE_INSTANCE" "$DEST_SERVICE_KEY"

CREDS_JSON=$(cf service-key "$DEST_SERVICE_INSTANCE" "$DEST_SERVICE_KEY" 2>/dev/null | tail -n +2)
CREDS_FILE=$(mktemp); trap "rm -f '$CREDS_FILE'" EXIT
python3 -c "
import json, sys, shlex
d = json.loads(sys.stdin.read()); c = d.get('credentials', d)
u = c.get('uaa', c)
print(f'DEST_API_URI={shlex.quote(c.get(\"uri\",\"\"))}')
print(f'DEST_CLIENT_ID={shlex.quote(u.get(\"clientid\",\"\"))}')
print(f'DEST_CLIENT_SECRET={shlex.quote(u.get(\"clientsecret\",\"\"))}')
print(f'DEST_TOKEN_URL={shlex.quote(u.get(\"url\",\"\"))}')
" <<< "$CREDS_JSON" > "$CREDS_FILE"
source "$CREDS_FILE"; rm -f "$CREDS_FILE"; trap - EXIT

[[ -n "$DEST_API_URI" && -n "$DEST_CLIENT_ID" && -n "$DEST_CLIENT_SECRET" && -n "$DEST_TOKEN_URL" ]] \
  || { echo "Error: could not extract destination service credentials"; exit 1; }

# ---- 4. OAuth token for the Destination Service API ----
echo "[4/5] Authenticating with the Destination Service..."
ACCESS_TOKEN=$(curl -s -X POST "${DEST_TOKEN_URL}/oauth/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=client_credentials" \
  -d "client_id=${DEST_CLIENT_ID}" \
  -d "client_secret=${DEST_CLIENT_SECRET}" \
  | python3 -c "import json,sys; print(json.load(sys.stdin).get('access_token',''))")
[[ -n "$ACCESS_TOKEN" ]] || { echo "Error: no OAuth token"; exit 1; }

# ---- 5. create / update the destination ----
echo "[5/5] Writing destination '${DESTINATION_NAME}'..."
PAYLOAD=$(cat <<JSON
{
  "Name": "${DESTINATION_NAME}",
  "Type": "HTTP",
  "URL": "${AGENT_URL}",
  "ProxyType": "Internet",
  "Authentication": "${AUTH_TYPE}",
  "HTML5.DynamicDestination": "true",
  "WebIDEEnabled": "true",
  "Description": "Joule -> A2A agent: ${AGENT_NAME}"
}
JSON
)

EXISTS=$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  "${DEST_API_URI}/destination-configuration/v1/subaccountDestinations/${DESTINATION_NAME}")

if [[ "$EXISTS" == "200" ]]; then
  RESP=$(curl -s -w "\n%{http_code}" -X PUT \
    -H "Authorization: Bearer ${ACCESS_TOKEN}" -H "Content-Type: application/json" \
    -d "$PAYLOAD" "${DEST_API_URI}/destination-configuration/v1/subaccountDestinations/${DESTINATION_NAME}")
else
  RESP=$(curl -s -w "\n%{http_code}" -X POST \
    -H "Authorization: Bearer ${ACCESS_TOKEN}" -H "Content-Type: application/json" \
    -d "$PAYLOAD" "${DEST_API_URI}/destination-configuration/v1/subaccountDestinations")
fi

RESP_CODE=$(tail -n1 <<< "$RESP")
if [[ "$RESP_CODE" =~ ^20[0-9]$ ]]; then
  echo "OK — ${DESTINATION_NAME} -> ${AGENT_URL} (${AUTH_TYPE})"
  echo "Next: cd joule-capability && joule deploy ./da.sapdas.yaml --compile -n finance_ai_chatbot_a2a"
else
  echo "Error: Destination API HTTP ${RESP_CODE}"
  head -n -1 <<< "$RESP"
  exit 1
fi
