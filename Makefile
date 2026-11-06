VENV ?= .venv
PY   := $(VENV)/bin/python

setup:
	python3 -m venv $(VENV) && $(VENV)/bin/pip install -r agent/requirements-dev.txt -r erp/requirements.txt

test:
	cd agent && ../$(PY) -m pytest tests/ -q

# Portao de cobertura: policy exige 100%. Falha o alvo se cair abaixo disso.
cov:
	cd agent && ../$(PY) -m pytest tests/ -q --cov=src --cov-report=term-missing --cov-fail-under=90
	cd agent && ../$(PY) -m pytest tests/test_policy.py -q --cov=src/policy --cov-fail-under=100

lint:
	$(VENV)/bin/ruff check agent/src agent/tests erp/app
	$(VENV)/bin/ruff format --check agent/src agent/tests erp/app

erp:
	cd erp && ../$(VENV)/bin/uvicorn app.main:app --host 127.0.0.1 --port 8080 --reload

.PHONY: setup test cov lint erp
