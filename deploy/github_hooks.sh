#!/usr/bin/env bash
# Point GitHub webhooks at the bot's Hermes routes, using the "repos" map in config.json.
#
#   deploy/github_hooks.sh                 # every repo in config.json
#   deploy/github_hooks.sh owner/repo      # just one
#
# Replaces any older bot hook on each repo. Needs: gh logged in as a repo admin, ssh access to the Hermes
# host (to read route secrets), and the bot account added as a collaborator with write access.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG="${CONFIG:-$ROOT/config.json}"
[[ -f "$CONFIG" ]] || { echo "No $CONFIG. Copy config.example.json to config.json and fill it in." >&2; exit 1; }

# repo route profile base host key, one line per repo (portable: macOS ships bash 3.2, no mapfile)
ROWS="$(python3 - "$CONFIG" "${1:-}" <<'PY'
import json, os, sys
c = json.load(open(sys.argv[1])); only = sys.argv[2]
h = c["hermes"]
for repo, route in c["repos"].items():
    if only and repo != only:
        continue
    prof = c["routes"][route].get("profile") or "-"
    print(repo, route, prof, h["webhook_base"], h["ssh_host"], os.path.expanduser(h["ssh_key"]))
PY
)"
[[ -n "$ROWS" ]] || { echo "No matching repos in $CONFIG" >&2; exit 1; }

while read -r REPO ROUTE PROFILE BASE HOST KEY; do
  SECRET=$(ssh -n -i "$KEY" -o BatchMode=yes "$HOST" \
    "python3 -c \"import yaml;print(yaml.safe_load(open('/root/.hermes/config.yaml'))['platforms']['webhook']['extra']['routes']['$ROUTE']['secret'])\"")
  # if/else, not `[[ ]] && echo`: a false test makes the substitution exit 1 and set -e aborts silently
  PREFIX=""; if [[ $PROFILE != - ]]; then PREFIX="/p/$PROFILE"; fi
  URL="$BASE$PREFIX/webhooks/$ROUTE"

  for id in $(gh api "repos/$REPO/hooks" </dev/null --jq ".[] | select(.config.url|test(\"/webhooks/$ROUTE\$\")) | .id"); do
    gh api -X DELETE "repos/$REPO/hooks/$id" </dev/null && echo "$REPO: replaced old hook $id"
  done
  gh api "repos/$REPO/hooks" -X POST -f name=web -F active=true </dev/null \
    -f "events[]=issues" -f "events[]=issue_comment" -f "events[]=pull_request" \
    -f "events[]=pull_request_review" -f "events[]=pull_request_review_comment" \
    -f "config[url]=$URL" -f config[content_type]=json -f "config[secret]=$SECRET" \
    --jq '"\(.id) -> \(.config.url)"' | sed "s|^|$REPO: |"
done <<<"$ROWS"
