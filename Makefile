VENV    := .venv
PYTHON  := $(VENV)/bin/python
PIP     := $(VENV)/bin/pip
UVICORN := $(VENV)/bin/uvicorn
PORT    ?= 8080

.DEFAULT_GOAL := help

.PHONY: help install dev run kb adduser

help:
	@echo ""
	@echo "  make install          create .venv and install dependencies"
	@echo "  make dev              start with auto-reload (local dev)"
	@echo "  make run              start without auto-reload (production)"
	@echo "  make kb               open the admin CLI (python kb.py)"
	@echo "  make adduser          create a web-only user  (EMAIL=foo@bar.com)"
	@echo ""
	@echo "  PORT=8080 make dev    override the default port"
	@echo ""

# Only create the venv if one doesn't already exist. Running `python3 -m venv`
# on top of an existing venv from inside an activated shell (VS Code does this
# automatically) leaves the venv in a half-rebuilt state. To force a clean
# rebuild, delete .venv manually.
$(VENV)/.installed: requirements.txt
	@test -x $(PYTHON) || python3 -m venv $(VENV)
	$(PIP) install --upgrade pip --quiet
	$(PIP) install -r requirements.txt --quiet
	$(VENV)/bin/playwright install chromium --with-deps
	@touch $(VENV)/.installed

install: $(VENV)/.installed

dev: install
	$(UVICORN) main:app --reload --host 0.0.0.0 --port $(PORT)

run: install
	$(UVICORN) main:app --host 0.0.0.0 --port $(PORT)

kb: install
	$(PYTHON) kb.py $(ARGS)

adduser: install
ifndef EMAIL
	$(error EMAIL is not set — usage: make adduser EMAIL=alice@example.com)
endif
	$(PYTHON) kb.py adduser $(EMAIL)
