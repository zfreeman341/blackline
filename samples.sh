#!/usr/bin/env bash
# Demo walkthrough: every change is a proposal against a specific document
# version; applying it is a separate, atomic, recorded act.
#
# Runs with zero API keys (MOCK_LLM=true is the default). If no server is
# listening on $BASE_URL, one is started for the duration of the script.
set -euo pipefail
cd "$(dirname "$0")"

BASE="${BASE_URL:-http://127.0.0.1:8000}"

banner()  { printf '\n\n════ %s ════\n\n' "$*"; }
pretty()  { python3 -m json.tool; }
field()   { python3 -c "import sys, json; print(json.load(sys.stdin)[\"$1\"])"; }

if ! curl -s -o /dev/null "$BASE/openapi.json"; then
    echo "No server on $BASE, starting one (MOCK_LLM=true)..."
    MOCK_LLM=true uv run uvicorn app.main:app --port "${PORT:-8000}" --log-level warning &
    SERVER_PID=$!
    trap 'kill "$SERVER_PID" 2>/dev/null || true' EXIT
    for _ in $(seq 1 50); do
        curl -s -o /dev/null "$BASE/openapi.json" && break
        sleep 0.1
    done
fi

banner "1) Create a document from seed contract text (revision 1 of its audit trail)"
CREATE_RESPONSE=$(python3 -c 'import json; print(json.dumps({"title": "Master Services Agreement", "text": open("seed/msa.txt").read()}))' \
    | curl -s -X POST "$BASE/documents" -H 'Content-Type: application/json' -d @-)
DOC_ID=$(echo "$CREATE_RESPONSE" | field id)
echo "$CREATE_RESPONSE" | field title
echo "created document $DOC_ID at version $(echo "$CREATE_RESPONSE" | field version)"

banner "2) Search across documents: snippets with context, offsets you can act on"
curl -s "$BASE/documents/search?q=termination&limit=5" | pretty

banner "3) Apply an unambiguous change: full updated text + new version come back"
curl -s -X PATCH "$BASE/documents/$DOC_ID" -H 'Content-Type: application/json' -d '{
  "changes": [
    { "operation": "replace",
      "target": { "text": "one percent (1%)" },
      "replacement": "one and one-half percent (1.5%)" }
  ]
}' | pretty

banner "4) Attempt an ambiguous change: 'thirty (30) days' appears in BOTH the payment and termination clauses. The API refuses to guess: 422 + candidates"
curl -s -X PATCH "$BASE/documents/$DOC_ID" -H 'Content-Type: application/json' -d '{
  "changes": [
    { "operation": "replace",
      "target": { "text": "thirty (30) days" },
      "replacement": "sixty (60) days" }
  ]
}' | pretty

banner "5) Disambiguate with occurrence: 2 (the termination-notice clause) and apply"
curl -s -X PATCH "$BASE/documents/$DOC_ID" -H 'Content-Type: application/json' -d '{
  "changes": [
    { "operation": "replace",
      "target": { "text": "thirty (30) days", "occurrence": 2 },
      "replacement": "sixty (60) days" }
  ]
}' | field version | sed 's/^/applied: document is now at version /'

banner "6) A stale write: expected_version=1, but the document has moved on -> 409"
curl -s -X PATCH "$BASE/documents/$DOC_ID" -H 'Content-Type: application/json' -d '{
  "expected_version": 1,
  "changes": [
    { "operation": "replace",
      "target": { "text": "Bluestone" },
      "replacement": "Redstone" }
  ]
}' | pretty

banner "7) Propose a change in natural language: the LLM suggests, NOTHING is applied"
PROPOSAL=$(curl -s -X POST "$BASE/documents/$DOC_ID/changes/propose" \
    -H 'Content-Type: application/json' \
    -d '{"instruction": "change the governing law from New York to Delaware"}')
echo "$PROPOSAL" | pretty
PROPOSAL_ID=$(echo "$PROPOSAL" | field proposal_id)
BASE_VERSION=$(echo "$PROPOSAL" | field base_version)

banner "8) Human reviews, then applies the proposal via the SAME PATCH path, pinned to the version it was validated against, carrying its proposal_id"
echo "$PROPOSAL" | python3 -c "
import sys, json
p = json.load(sys.stdin)
print(json.dumps({
    'expected_version': p['base_version'],
    'proposal_id': p['proposal_id'],
    'changes': p['changes'],
}))" | curl -s -X PATCH "$BASE/documents/$DOC_ID" -H 'Content-Type: application/json' -d @- \
     | field version | sed 's/^/applied: document is now at version /'

banner "9) The audit trail: every revision records its base version, the change set as submitted, and its provenance: human edits and the applied LLM proposal, side by side"
curl -s "$BASE/documents/$DOC_ID/revisions" | pretty
