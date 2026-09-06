#!/bin/bash
# SessionStart hook — CLOUD SESSIONS ONLY (Claude Code on the web / cloud VM).
#
# Brings a fresh cloud VM to parity with the laptop:
#   1. Python packages the bots and analysis code import.
#   2. BigQuery credentials materialized from an environment variable set on the
#      cloud environment (claude.ai/code -> environment settings -> Environment
#      variables), exported for the session via CLAUDE_ENV_FILE.
#   3. A capability report (BigQuery creds? Kalshi key? Kalshi host reachable?)
#      so the session knows what it can and cannot do before it starts work.
#
# Exits immediately on local sessions (CLAUDE_CODE_REMOTE is only "true" on the
# cloud VM), so the laptop setup is untouched. Idempotent. Never prints secret
# values. Always exits 0 — a failed install must not block the session.
#
# Cloud environment variables this reads (all optional):
#   GCP_SA_KEY          service-account JSON (single line) or base64 of it. Use a
#                       READ-ONLY SA (roles/bigquery.dataViewer + jobUser); the
#                       GitHub Actions key writes tables and should stay there.
#   KALSHI_API_KEY_ID   Kalshi API key id (scripts default to the live id).
#   KALSHI_PRIVATE_KEY  base64 PEM (or raw PEM). Read directly by the scripts
#                       via load_private_key(); nothing is written to disk.
#
# See CLAUDE.md ("Where things run") for the local / cloud / GitHub Actions split.
set -uo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
cd "$ROOT" || exit 0

status=()

# ---------------------------------------------------------------------------
# 1) Python dependencies (union of the GH workflow installs + analysis code)
# ---------------------------------------------------------------------------
PKGS="pandas numpy scipy plotly requests cryptography pytz python-dateutil google-auth google-cloud-bigquery db-dtypes pyarrow"
if python3 - <<'PY' 2>/dev/null
import pandas, numpy, scipy, plotly, requests, cryptography, pytz, dateutil
import google.auth, google.cloud.bigquery, db_dtypes, pyarrow
PY
then
  status+=("pydeps=present")
else
  # --ignore-installed: the base image's Debian-managed 'packaging' has no pip
  # RECORD file, so a plain install that tries to upgrade it aborts midway.
  # shellcheck disable=SC2086
  if pip install --quiet --disable-pip-version-check --ignore-installed packaging $PKGS \
       >/tmp/session-start-pip.log 2>&1; then
    status+=("pydeps=installed")
  else
    status+=("pydeps=FAILED(see /tmp/session-start-pip.log)")
  fi
fi

# ---------------------------------------------------------------------------
# 2) BigQuery credentials
# ---------------------------------------------------------------------------
if [ -n "${GOOGLE_APPLICATION_CREDENTIALS:-}" ] && [ -s "${GOOGLE_APPLICATION_CREDENTIALS}" ]; then
  status+=("bigquery=creds-file")
elif [ -n "${GCP_SA_KEY:-}" ]; then
  SA_FILE="$ROOT/gcp_sa_key.json"          # gitignored
  case "$GCP_SA_KEY" in
    \{*) printf '%s' "$GCP_SA_KEY" > "$SA_FILE" ;;
    *)   printf '%s' "$GCP_SA_KEY" | base64 -d > "$SA_FILE" 2>/dev/null || printf '%s' "$GCP_SA_KEY" > "$SA_FILE" ;;
  esac
  chmod 600 "$SA_FILE"
  if python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$SA_FILE" 2>/dev/null; then
    if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
      echo "export GOOGLE_APPLICATION_CREDENTIALS=\"$SA_FILE\"" >> "$CLAUDE_ENV_FILE"
    fi
    status+=("bigquery=creds-from-GCP_SA_KEY")
  else
    rm -f "$SA_FILE"
    status+=("bigquery=GCP_SA_KEY-not-valid-JSON(paste the SA JSON on one line, or base64 of it)")
  fi
else
  status+=("bigquery=NO-CREDS(set GCP_SA_KEY on the cloud environment)")
fi

# ---------------------------------------------------------------------------
# 3) Kalshi: key presence + network reachability (Trusted network level blocks
#    api.elections.kalshi.com; the environment needs Custom with that host).
# ---------------------------------------------------------------------------
if [ -n "${KALSHI_PRIVATE_KEY:-}" ]; then
  status+=("kalshi_key=present")
else
  status+=("kalshi_key=absent(GitHub-Actions probe pattern still works)")
fi
code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 8 \
       "https://api.elections.kalshi.com/trade-api/v2/exchange/status" 2>/dev/null || true)
if [ "$code" = "200" ]; then
  status+=("kalshi_net=reachable")
else
  status+=("kalshi_net=BLOCKED(http=${code:-000}; env network -> Custom + api.elections.kalshi.com)")
fi

echo "[session-start] cloud parity: ${status[*]}"
exit 0
