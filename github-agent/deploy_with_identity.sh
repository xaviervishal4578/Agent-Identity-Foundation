#!/usr/bin/env bash
#
# Deploys an ADK agent to Vertex AI Agent Runtime with Agent Identity
# (Preview) enabled, using raw REST calls instead of `agents-cli deploy
# --agent-identity` or `adk deploy agent_engine`.
#
# WHY THIS SCRIPT EXISTS: as of agents-cli<=1.2.1 and google-adk<=2.5.0, both
# clients set `identityType` on the SAME request that also deploys the
# source/image code, and the backend rejects that combination every time
# ("Reasoning Engine resource [...] failed to start and cannot serve
# traffic"). The fix is to split it into two calls: (1) create the engine
# with identityType only, nothing else, so the platform can cleanly
# provision the federated identity; (2) deploy the code in a separate update
# that never touches identityType again. See agent-identity-deploy-findings.md
# next to this script for the full writeup.
#
# Usage:
#   ./deploy_with_identity.sh
#
# Config is read from environment variables (all optional, sensible
# defaults shown) and from the project's own .env file for agent env vars.
#
#   PROJECT_ID          GCP project id.                 default: gcloud's current project
#   REGION              Region for the engine.          default: us-central1
#   DISPLAY_NAME        Reasoning Engine name.           default: name of this directory
#   SOURCE_DIR          Agent project to deploy.         default: this script's directory
#   A2A_APP_NAME         app_name your source registers   default: app
#                        A2A routes under (cosmetic only — used for the
#                        printed test command / agent card URL).
#   CONNECTOR_NAME       If set, also grants this run's identity            (unset)
#                        roles/iamconnectors.user on this Agent Identity
#                        connector (needed for any MCP/tool call that
#                        retrieves credentials from it).
#   CONNECTOR_LOCATION   Location of CONNECTOR_NAME.      default: $REGION

set -euo pipefail

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DIR="${SOURCE_DIR:-$SCRIPT_DIR}"
PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-us-central1}"
DISPLAY_NAME="${DISPLAY_NAME:-$(basename "$SOURCE_DIR")}"
# Must match the app_name your source uses to register A2A routes (e.g. the
# APP_NAME constant in a hand-written main.py, or the App(name=...) value
# for an agents-cli-scaffolded project). Only used for the printed test
# command / agent card URL below — does not affect the deploy itself.
A2A_APP_NAME="${A2A_APP_NAME:-app}"

if [[ -z "$PROJECT_ID" ]]; then
  echo "ERROR: no project set. Run 'gcloud config set project <id>' or pass PROJECT_ID=..." >&2
  exit 1
fi

log() { echo -e "$1"; }
die() { echo -e "❌ $1" >&2; exit 1; }

log "🔧 Resolving project details for ${PROJECT_ID}..."
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
ORG_ID="$(gcloud projects get-ancestors "$PROJECT_ID" --format='value(id,type)' | awk '$2=="organization"{print $1}')"
[[ -n "$PROJECT_NUMBER" ]] || die "could not resolve project number for $PROJECT_ID"
[[ -n "$ORG_ID" ]] || die "could not resolve organization id for $PROJECT_ID (needed for the identity principal)"

API_BASE="https://${REGION}-aiplatform.googleapis.com/v1beta1/projects/${PROJECT_NUMBER}/locations/${REGION}"
AUTH_HEADER() { echo "Authorization: Bearer $(gcloud auth print-access-token)"; }

log "   Project number: ${PROJECT_NUMBER}"
log "   Org id:         ${ORG_ID}"
log "   Region:         ${REGION}"
log "   Display name:   ${DISPLAY_NAME}"
log "   Source dir:     ${SOURCE_DIR}"

# ---------------------------------------------------------------------------
# Poll a long-running operation until done. Prints the final JSON and exits
# non-zero if the operation completed with an error.
# ---------------------------------------------------------------------------
poll_operation() {
  local op_name="$1"
  local label="$2"
  local attempt=0
  while true; do
    attempt=$((attempt + 1))
    local result
    result="$(curl -s -H "$(AUTH_HEADER)" "https://${REGION}-aiplatform.googleapis.com/v1beta1/${op_name}")"
    local done_flag
    done_flag="$(echo "$result" | python3 -c "import json,sys; print(json.load(sys.stdin).get('done', False))" 2>/dev/null || echo False)"
    if [[ "$done_flag" == "True" ]]; then
      local err
      err="$(echo "$result" | python3 -c "import json,sys; d=json.load(sys.stdin); print(json.dumps(d['error']) if 'error' in d else '')")"
      if [[ -n "$err" ]]; then
        echo ""
        die "$label failed: $err"
      fi
      echo "$result"
      return 0
    fi
    printf "\r   %s... (%ds)" "$label" "$((attempt * 10))" >&2
    sleep 10
  done
}

# ---------------------------------------------------------------------------
# Step 0 — required APIs
# ---------------------------------------------------------------------------
log "\n🔌 Enabling required APIs (skips ones already on)..."
gcloud services enable \
  aiplatform.googleapis.com \
  agentidentity.googleapis.com \
  agentidentitycredentials.googleapis.com \
  agentidentity.googleapis.com \
  agentidentitycredentials.googleapis.com \
  iamcredentials.googleapis.com \
  sts.googleapis.com \
  --project="$PROJECT_ID" >/dev/null

# ---------------------------------------------------------------------------
# Step 1 — find or create the engine with identity set (identity ONLY, no
# code). If it already exists, we reuse it and skip straight to Step 3 so
# re-running this script for a code change doesn't re-provision identity.
# ---------------------------------------------------------------------------
log "\n🔍 Checking for an existing engine named '${DISPLAY_NAME}'..."
EXISTING="$(curl -s -H "$(AUTH_HEADER)" "${API_BASE}/reasoningEngines" \
  | python3 -c "
import json, sys
d = json.load(sys.stdin)
for e in d.get('reasoningEngines', []):
    if e.get('displayName') == '${DISPLAY_NAME}':
        print(e['name'])
        break
")"

if [[ -n "$EXISTING" ]]; then
  ENGINE_NAME="$EXISTING"
  ENGINE_ID="$(basename "$ENGINE_NAME")"
  log "   Found existing engine: ${ENGINE_ID}"
  EFFECTIVE_IDENTITY="$(curl -s -H "$(AUTH_HEADER)" "https://${REGION}-aiplatform.googleapis.com/v1beta1/${ENGINE_NAME}" \
    | python3 -c "import json,sys; print(json.load(sys.stdin).get('spec',{}).get('effectiveIdentity',''))")"
  if [[ "$EFFECTIVE_IDENTITY" != *system.id.goog* ]]; then
    die "engine ${ENGINE_ID} exists but has no federated identity (effectiveIdentity='${EFFECTIVE_IDENTITY}'). Delete it and re-run, or use a different DISPLAY_NAME."
  fi
else
  log "🔧 Creating a new engine with Agent Identity..."
  CREATE_OP="$(curl -s -X POST -H "$(AUTH_HEADER)" -H "Content-Type: application/json" \
    "${API_BASE}/reasoningEngines" \
    -d "{\"displayName\": \"${DISPLAY_NAME}\", \"spec\": {\"identityType\": \"AGENT_IDENTITY\"}}" \
    | python3 -c "import json,sys; print(json.load(sys.stdin)['name'])")"

  CREATE_RESULT="$(poll_operation "$CREATE_OP" "Provisioning identity")"
  ENGINE_NAME="$(echo "$CREATE_RESULT" | python3 -c "import json,sys; print(json.load(sys.stdin)['response']['name'])")"
  ENGINE_ID="$(basename "$ENGINE_NAME")"
  EFFECTIVE_IDENTITY="$(echo "$CREATE_RESULT" | python3 -c "import json,sys; print(json.load(sys.stdin)['response']['spec']['effectiveIdentity'])")"
  log "\n   ✅ Engine created: ${ENGINE_ID}"
fi

log "   Effective identity: ${EFFECTIVE_IDENTITY}"
PRINCIPAL="principal://${EFFECTIVE_IDENTITY}"

# ---------------------------------------------------------------------------
# Step 2 — grant the roles the identity needs. Idempotent: safe to re-run.
# ---------------------------------------------------------------------------
log "\n🔐 Granting IAM roles to the agent's identity..."
for ROLE in roles/aiplatform.user roles/serviceusage.serviceUsageConsumer \
            roles/browser roles/cloudapiregistry.viewer \
            roles/logging.logWriter roles/monitoring.metricWriter \
            roles/agentidentity.user; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="$PRINCIPAL" --role="$ROLE" --condition=None >/dev/null 2>&1
done
log "   ✅ Roles granted"

# ---------------------------------------------------------------------------
# Step 3 — build the source tarball, honoring .gcloudignore/.gitignore if
# present, and deploy the code. `identityType` is deliberately excluded from
# the update mask below — that's the whole fix.
# ---------------------------------------------------------------------------
log "\n📦 Packaging source from ${SOURCE_DIR}..."
IGNORE_FILE=""
[[ -f "${SOURCE_DIR}/.gcloudignore" ]] && IGNORE_FILE="${SOURCE_DIR}/.gcloudignore"
[[ -z "$IGNORE_FILE" && -f "${SOURCE_DIR}/.gitignore" ]] && IGNORE_FILE="${SOURCE_DIR}/.gitignore"

TAR_EXCLUDES=(--exclude='.venv' --exclude='__pycache__' --exclude='.git' --exclude='*.pyc' --exclude='.env')
if [[ -n "$IGNORE_FILE" ]]; then
  while IFS= read -r pattern; do
    [[ -z "$pattern" || "$pattern" == \#* ]] && continue
    TAR_EXCLUDES+=(--exclude="${pattern%/}")
  done < "$IGNORE_FILE"
fi

TARBALL="$(mktemp -t source-XXXXXX.tar.gz)"
tar "${TAR_EXCLUDES[@]}" -czf "$TARBALL" -C "$SOURCE_DIR" .
log "   Tarball: $(du -h "$TARBALL" | cut -f1)"

log "\n📝 Building deploy request body..."
DEPLOY_BODY="$(mktemp -t deploy-body-XXXXXX.json)"
python3 -c "
import base64, json, os, sys

source_dir = '${SOURCE_DIR}'
tarball = '${TARBALL}'
project = '${PROJECT_ID}'

with open(tarball, 'rb') as f:
    b64 = base64.b64encode(f.read()).decode('utf-8')

env = []
env_path = os.path.join(source_dir, '.env')
if os.path.exists(env_path):
    for line in open(env_path):
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        key = key.strip()
        value = value.strip().strip('\"').strip(\"'\")
        if key == 'GOOGLE_CLOUD_PROJECT':
            continue  # reserved, the platform injects this itself
        env.append({'name': key, 'value': value})

body = {
    'displayName': '${DISPLAY_NAME}',
    'spec': {
        'sourceCodeSpec': {
            'inlineSource': {'sourceArchive': b64},
            'imageSpec': {},
        },
        'deploymentSpec': {
            'env': env,
            'minInstances': 1,
            'maxInstances': 10,
            'resourceLimits': {'cpu': '1', 'memory': '4Gi'},
            'containerConcurrency': 8,
        },
        'agentFramework': 'google-adk',
    },
}
json.dump(body, open('${DEPLOY_BODY}', 'w'))
print(f'   {len(env)} env vars from .env included')
"

log "\n🚀 Deploying code to ${ENGINE_ID} (identityType intentionally left untouched)..."
UPDATE_MASK="displayName,spec.sourceCodeSpec.inlineSource.sourceArchive,spec.sourceCodeSpec.imageSpec,spec.deploymentSpec.env,spec.deploymentSpec.minInstances,spec.deploymentSpec.maxInstances,spec.deploymentSpec.resourceLimits,spec.deploymentSpec.containerConcurrency,spec.agentFramework"
UPDATE_OP="$(curl -s -X PATCH -H "$(AUTH_HEADER)" -H "Content-Type: application/json" \
  "${API_BASE}/reasoningEngines/${ENGINE_ID}?updateMask=${UPDATE_MASK}" \
  -d @"$DEPLOY_BODY" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('name') or ('ERROR:' + json.dumps(d)))")"

rm -f "$TARBALL" "$DEPLOY_BODY"

if [[ "$UPDATE_OP" == ERROR:* ]]; then
  die "deploy request rejected: ${UPDATE_OP#ERROR:}"
fi

poll_operation "$UPDATE_OP" "Deploying (this can take 5-10 min)" >/dev/null
log "\n   ✅ Deployment successful!"

CARD_URL="https://${REGION}-aiplatform.googleapis.com/reasoningEngines/v1/${ENGINE_NAME}/api/a2a/${A2A_APP_NAME}/.well-known/agent-card.json"
LOGS_URL="https://console.cloud.google.com/logs/query?project=${PROJECT_ID}&query=resource.type%3D%22aiplatform.googleapis.com%2FReasoningEngine%22%0Aresource.labels.reasoning_engine_id%3D%22${ENGINE_ID}%22"

log "\n🪪 Agent Card URL: ${CARD_URL}"
log "🆔 Reasoning Engine ID: ${ENGINE_ID}"
log "📋 Logs: ${LOGS_URL}"

if [[ -n "${CONNECTOR_NAME:-}" ]]; then
  log "\n🔌 Granting the agent's identity access to connector '${CONNECTOR_NAME}'..."
  gcloud alpha agent-identity connectors add-iam-policy-binding "$CONNECTOR_NAME" \
    --project="$PROJECT_ID" --location="${CONNECTOR_LOCATION:-$REGION}" \
    --role="roles/iamconnectors.user" --member="$PRINCIPAL" >/dev/null 2>&1
  log "   ✅ Granted (set CONNECTOR_NAME=<name> [CONNECTOR_LOCATION=<loc>] to auto-grant this on every run)"
fi

log "\nTest it with (note --app-name is required outside an agents-cli-scaffolded project):"
log "  agents-cli run --url \"https://${REGION}-aiplatform.googleapis.com/v1/${ENGINE_NAME}\" --mode a2a --app-name ${A2A_APP_NAME} \"What can you do?\""
