# Outbound Dialing SOP (Live Omnichannel Workflow)

## Purpose

Local reference for the end-to-end live outbound loop in this repo:

- lead acquisition → campaign queue → Retell batch calls → journey normalization → n8n/Twilio follow-ups → Supabase upserts

## Source and campaign setup

- Default lead source: `compass/crawler-google-places` (Apify)
- Default campaign: `ont-live-001`
- Campaign name used in metadata: `b2b_outbound_workflow`
- Default states: `Texas,Florida,California`
- Default segment signals:
  - review score
  - review count
  - employee count
  - location / state
  - website presence
  - manager/decision-maker contact

## Campaign caps and stop logic

- Daily call cap: `CAMPAIGN_DAILY_CALL_CAP` (default `3`)
- Max attempts per lead: `CAMPAIGN_MAX_ATTEMPTS` (default `500`)
- Attempt warning threshold: `CAMPAIGN_ATTEMPT_WARNING_THRESHOLD` (default `200`)
  - leads above threshold are flagged with `attempts_exceeded_200=true`
- Stop reasons (hard skip):
  - `dnc,closed,invalid,contacted,booked`
- N8N workflow contract:
  - Canonical outbound workflow: `openclaw_retell_call_dispatch`
  - Legacy/auxiliary workflow: `openclaw_retell_fn_b2c_quote` (do not use for outbound dispatch)
  - Dispatch webhook: `N8N_B2B_DISPATCH_WEBHOOK` (`/webhook/openclaw-retell-dispatch`)
- Resume behavior:
  - `--resume` skips leads already present in dispatch state

## Runtime commands

```bash
make live-build-queue
make live-call-batch
make live-map-journeys
make live-export-supabase
```

Common direct calls:

```bash
python3 scripts/build_live_campaign_queue.py \
  --campaign-id ont-live-001 \
  --campaign-name b2b_outbound_workflow \
  --states Texas,Florida,California \
  --out-dir data/leads \
  --top-k 500 \
  --query medspa

python3 scripts/run_live_campaign.py \
  --queue-file data/leads/live_call_queue.jsonl \
  --out-dir data/retell_calls \
  --campaign-id ont-live-001 \
  --tenant live_medspa \
  --max-attempts 500 \
  --attempt-warning-threshold 200 \
  --daily-call-cap 3 \
  --resume \
  --limit-call-rate

python3 scripts/synthetic_journey_mapper.py \
  --calls-dir data/retell_calls \
  --lead-file data/leads/live_leads.csv \
  --campaign-id ont-live-001 \
  --tenant live_medspa \
  --out data/retell_calls/live_customer_journeys.jsonl

python3 scripts/export_journey_to_supabase.py \
  --calls-dir data/retell_calls \
  --journey-path data/retell_calls/live_customer_journeys.jsonl \
  --supabase-url "$SUPABASE_URL" \
  --supabase-key "$SUPABASE_SERVICE_KEY" \
  --schema "$SUPABASE_SCHEMA"
```

## Environment contract (.env)

Use `.env.retell.example` as the source of truth and copy values into `.env.retell.local`:

- Retell
  - `RETELL_API_KEY`
  - `B2B_AGENT_ID`
  - `RETELL_FROM_NUMBER`
- Apify
  - `APIFY_ACTOR_ID`
  - `APIFY_API_TOKEN`
- n8n
  - `N8N_LEAD_WEBHOOK_URL`
  - `N8N_B2B_DISPATCH_WORKFLOW`
  - `N8N_B2B_DISPATCH_WEBHOOK`
  - `N8N_B2B_OUTCOME_WORKFLOW`
  - `N8N_B2B_OUTCOME_WEBHOOK`
  - `N8N_OUTCOME_WEBHOOK_URL`
- Supabase
  - `SUPABASE_URL`
  - `SUPABASE_SERVICE_KEY`
  - `SUPABASE_SCHEMA`
- Campaign policy
  - `CAMPAIGN_DAILY_CALL_CAP=3`
  - `CAMPAIGN_MAX_ATTEMPTS=500`
  - `CAMPAIGN_ATTEMPT_WARNING_THRESHOLD=200`
- Twilio credentials (used by n8n actions):
  - `TWILIO_ACCOUNT_SID`
  - `TWILIO_AUTH_TOKEN`
  - `TWILIO_API_KEY_SID`
  - `TWILIO_API_KEY_SECRET`

## Notes for future rollout

- Keep `attempts_exceeded_200=true` as a first-class journey attribute and route high-attempt leads into a separate nurture sequence.
- Keep existing call scripts and n8n integrations additive; deprecate old outbound workflows by aliasing to this contract and updating triggers only.
