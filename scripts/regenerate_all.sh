#!/usr/bin/env bash
# Rebuild every model-produced artifact and the automated evidence from scratch.
# Needs: `rote serve` running, ANTHROPIC_API_KEY. Costs two short discovery runs (~12 model turns total).
# Not automated (need a human at the console): evidence/04-human-handoff. See evidence/README.md.
set -euo pipefail
cd "$(dirname "$0")/.."
R=.venv/bin/rote
rm -rf capabilities/keystone.coreserv.member_savings_balance capabilities/keystone.coreserv.open_sub_account_review \
       capabilities/keystone.coreserv.open_sub_account
$R discover --tenant prairie --reset --capability-id keystone.coreserv.member_savings_balance \
  --goal "Look up member 12345 and read their current savings balance (the REGULAR SHARES account)."
$R discover --tenant prairie --reset --capability-id keystone.coreserv.open_sub_account_review \
  --goal "For member 48213, start opening a new SHARE CERTIFICATE 12 MO sub-account with nickname COLLEGE FUND and an opening deposit of 500.00 funded from suffix 0000, and reach the review screen. Do not open the account."
.venv/bin/python scripts/review_edits.py
$R approve keystone.coreserv.member_savings_balance@1.0.0 --by reviewer.anay
$R approve keystone.coreserv.open_sub_account_review@1.0.0 --by reviewer.anay
.venv/bin/python scripts/author_open_sub_account.py
$R schema
.venv/bin/python scripts/make_evidence.py
$R catalog --tenant prairie > evidence/06-agent-catalog/catalog.prairie.json
