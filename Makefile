# Linda Mini fax-routing agent. Run `make help` for targets.
PORTAL_DIR ?= ../linda-mini
export LINDA_PORTAL_DIR := $(abspath $(PORTAL_DIR))
PORT       ?= 8000
RESULTS    ?= results/results.json
REPORT_OUT ?= $(dir $(RESULTS))report.html

.PHONY: help setup portal run demo test chaos eval perturb report

help:
	@echo "make setup    install deps (uv sync) + Chromium for Playwright"
	@echo "make portal   start the supplied portal on 127.0.0.1:$(PORT) (PORTAL_DIR=$(PORTAL_DIR))"
	@echo "make demo     run the agent in replay mode: real browser, committed vision cache, no API key"
	@echo "make run      run the agent live (Claude vision; needs ANTHROPIC_API_KEY in .env)"
	@echo "make test     pytest (unit + integration)"
	@echo "make chaos    fault-injection + kill/resume suite"
	@echo "make record   record a narrated demo video of a full run -> results/demo.mp4 (needs ffmpeg)"
	@echo "make eval     score RESULTS against eval/labels.yaml (human labels) -> eval/report.md"
	@echo "make perturb  drop/blank pages of downloaded PDFs and check the agent fails closed"
	@echo "make report   static HTML report for RESULTS -> $(REPORT_OUT)"

setup:
	uv sync
	uv run playwright install chromium
	@test -x $(PORTAL_DIR)/.venv/bin/uvicorn || (cd $(PORTAL_DIR) && python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt)
	@echo "Optional Tesseract cross-check: brew install tesseract  (or apt-get install tesseract-ocr)"

portal:
	@test -x $(PORTAL_DIR)/.venv/bin/uvicorn || (cd $(PORTAL_DIR) && python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt)
	cd $(PORTAL_DIR) && .venv/bin/uvicorn app:app --host 127.0.0.1 --port $(PORT)

run:
	uv run python -m agent run

demo:
	LINDA_OCR_MODE=replay uv run python -m agent run --replay

test:
	uv run pytest -q

record:
	uv run python -m harness.record_demo

chaos:
	uv run python -m harness.chaos

eval:
	uv run python -m eval.run_eval --results $(RESULTS)

perturb:
	uv run python -m eval.perturb --results $(RESULTS)

report:
	uv run python -m agent.html_report $(RESULTS) -o $(REPORT_OUT)
