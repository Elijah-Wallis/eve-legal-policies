.PHONY: help call call-status retell-fast ws-on ws-restore ws-dev cloudflare-verify learn leads ops-loop money test ci ci-local metrics dashboard go self-improve skill-capture skill-validate

help:
	@echo "Simple commands:"
	@echo "  make call                  # call default DOGFOOD_TO_NUMBER"
	@echo "  make call TO=+19859914360  # call a specific number"
	@echo "  make retell-fast           # patch live Retell B2B prompt + fastest reply settings"
	@echo "  make ws-on                 # switch B2B agent to Custom LLM WebSocket brain (requires BRAIN_WSS_BASE_URL in .env)"
	@echo "  make ws-restore BACKUP=...  # restore agent response engine from backup JSON"
	@echo "  make ws-dev                # local dev: start server + cloudflared + switch agent"
	@echo "  make cloudflare-verify     # verify Cloudflare API token from .env.cloudflare.local"
	@echo "  make learn                 # sync calls + transcripts/recordings + auto-refine at threshold"
	@echo "  make leads INPUT=...       # score/sort lead lists and optionally push to n8n"
	@echo "  make ops-loop              # compute objective metrics from call corpus + next actions"
	@echo "  make money                 # run learn + ops-loop + scorecard in one command"
	@echo "  make go                    # start server in dogfood mode (loads .env.retell.local, B2B profile)"
	@echo "  make call-status ID=call_x # fetch call status"
	@echo "  make test                  # run pytest -q"
	@echo "  make ci                    # run hard gate suite (backend + expressive + web)"
	@echo "  make ci-local              # run local hard gates without dependency install"
	@echo "  make metrics               # print key metric summary from /metrics"
	@echo "  make dashboard             # open Eve dashboard URL"
	@echo "  make self-improve          # run safe self-improvement cycle (propose)"
	@echo "  make skill-capture ID=... INTENT=... [TESTS=...]"
	@echo "  make skill-validate PATH=skills/<file>.md"

call:
	@./scripts/call_b2b.sh "$(TO)"

call-status:
	@./scripts/call_status.sh "$(ID)"

retell-fast:
	@./scripts/retell_fast_recover.sh

ws-on:
	@./scripts/b2b_switch_to_ws_brain.sh

ws-restore:
	@./scripts/retell_restore_agent.sh "$(BACKUP)"

ws-dev:
	@./scripts/ws_brain_dev_on.sh

cloudflare-verify:
	@./scripts/cloudflare_verify.sh

learn:
	@python3 scripts/retell_learning_loop.py --limit $${RETELL_LEARN_LIMIT:-100} --threshold $${RETELL_LEARN_THRESHOLD:-250}

leads:
	@python3 scripts/lead_factory.py --input "$(INPUT)" --out-dir $${LEAD_OUT_DIR:-data/leads} --min-score $${LEAD_MIN_SCORE:-60} --top-k $${LEAD_TOP_K:-500}

ops-loop:
	@python3 scripts/revenue_ops_loop.py --calls-dir $${OPS_CALLS_DIR:-data/retell_calls} --out-dir $${OPS_OUT_DIR:-data/revenue_ops}

money:
	@python3 scripts/retell_learning_loop.py --limit $${RETELL_LEARN_LIMIT:-100} --threshold $${RETELL_LEARN_THRESHOLD:-250}
	@python3 scripts/revenue_ops_loop.py --calls-dir $${OPS_CALLS_DIR:-data/retell_calls} --out-dir $${OPS_OUT_DIR:-data/revenue_ops}
	@URL=$${METRICS_URL:-http://127.0.0.1:8080/metrics}; \
	if curl -fsS "$$URL" >/dev/null 2>&1; then \
	  python3 scripts/dogfood_scorecard.py --metrics-url "$$URL"; \
	else \
	  echo "Skipping scorecard (metrics endpoint unreachable at $$URL)"; \
	fi

test:
	@python3 -m pytest -q

ci:
	@bash scripts/ci_hard_gates.sh

ci-local:
	@PY=".venv/bin/python"; \
	if [ ! -x "$$PY" ]; then PY="python3"; fi; \
	$$PY -m pytest -q tests tests_expressive; \
	$$PY -m pytest -q -k vic_contract; \
	$$PY -m pytest -q tests/acceptance/at_vic_100_sessions.py; \
	$$PY -m pytest -q tests/acceptance/at_no_leak_30min.py; \
	$$PY -m pytest -q tests/acceptance/at_ws_torture_5min.py; \
	if command -v npm >/dev/null 2>&1 && [ -f apps/web/package.json ]; then \
	  (cd apps/web && npm run test && npm run build); \
	else \
	  echo "Skipping web gates: npm/apps/web not available"; \
	fi

metrics:
	@python3 scripts/metrics_summary.py --metrics-url http://127.0.0.1:8080/metrics

dashboard:
	@bash scripts/run_dashboard.sh

go:
	@bash scripts/run_dashboard.sh

self-improve:
	@python3 scripts/self_improve_cycle.py --mode $${SELF_IMPROVE_MODE:-propose}

skill-capture:
	@python3 scripts/skills/capture_skill.py --id "$(ID)" --intent "$(INTENT)" --tests "$(TESTS)"

skill-validate:
	@python3 scripts/skills/validate_skill.py "$(PATH)"
