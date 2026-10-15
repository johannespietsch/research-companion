VENV     := .venv
PYTHON   := $(VENV)/bin/python
PIP      := $(VENV)/bin/pip
UVICORN  := $(VENV)/bin/uvicorn
# Must match the Dockerfile's base image (python:3.11-slim) so the venv
# resolves the same way prod does — see requirements.lock and issue #121.
PYTHON311 := python3.11
PORT     ?= 8080

.DEFAULT_GOAL := help

.PHONY: help install dev run test kb adduser scan-errors scan-errors-dry lock

help:
	@echo ""
	@echo "  make install          create .venv (Python 3.11) and install pinned dependencies"
	@echo "  make dev              start with auto-reload (local dev)"
	@echo "  make run              start without auto-reload (production)"
	@echo "  make test             run the pytest suite"
	@echo "  make lock             regenerate requirements.lock from requirements.in (needs uv)"
	@echo "  make kb               open the admin CLI (python kb.py)"
	@echo "  make adduser          create a web-only user  (EMAIL=foo@bar.com)"
	@echo "  make scan-errors-dry  classify last 24h of error_log, no GH writes"
	@echo "  make scan-errors      classify and file GH issues for new bugs"
	@echo ""
	@echo "  PORT=8080 make dev    override the default port"
	@echo ""

# Only create the venv if one doesn't already exist. Running `python3.11 -m
# venv` on top of an existing venv from inside an activated shell (VS Code
# does this automatically) leaves the venv in a half-rebuilt state. To force
# a clean rebuild, delete .venv manually.
#
# Installs from requirements.lock (fully pinned, hashed) rather than
# requirements.in, so local installs match the exact versions prod runs —
# see issue #121. Run `make lock` after editing requirements.in.
$(VENV)/.installed: requirements.lock
	@test -x $(PYTHON) || $(PYTHON311) -m venv $(VENV)
	$(PIP) install --upgrade pip --quiet
	$(PIP) install -r requirements.lock --quiet
	$(VENV)/bin/playwright install chromium --with-deps
	@touch $(VENV)/.installed

install: $(VENV)/.installed

dev: install
	$(UVICORN) main:app --reload --host 0.0.0.0 --port $(PORT)

run: install
	$(UVICORN) main:app --host 0.0.0.0 --port $(PORT)

test: install
	$(PYTHON) -m pytest tests/ -v

kb: install
	$(PYTHON) kb.py $(ARGS)

adduser: install
ifndef EMAIL
	$(error EMAIL is not set — usage: make adduser EMAIL=alice@example.com)
endif
	$(PYTHON) kb.py adduser $(EMAIL)

scan-errors-dry: install
	$(PYTHON) -m scripts.scan_errors --dry-run

scan-errors: install
	$(PYTHON) -m scripts.scan_errors

# --universal resolves one lock usable both inside the Dockerfile's target
# (Python 3.11, linux x86_64 — what prod actually runs) and on local dev
# machines (macOS/Linux), picking the right wheel per platform via markers.
# Requires `uv` (pip install uv). Regenerate after editing requirements.in,
# commit the result, then `make install` to pick it up locally.
#
# Known gap (see #121, "leave it" decision): onnxruntime (via faster-whisper)
# and cryptography (via pyjwt[crypto]) dropped Intel-macOS wheels upstream,
# so this lock — and `make install` — won't complete on an Intel Mac. Use
# Apple Silicon, Linux, or CI for local dev/test on this repo.
lock:
	@command -v uv >/dev/null 2>&1 || { echo "uv not found — install with: pip install uv"; exit 1; }
	uv pip compile requirements.in \
		--python-version 3.11 \
		--universal \
		--generate-hashes \
		--output-file requirements.lock
