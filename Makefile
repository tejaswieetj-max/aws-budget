.PHONY: install lint test synth diff deploy seed verify demo teardown research
export PYTHONPATH := services:$(PYTHONPATH)
PYTHON ?= .venv/bin/python
install:
	python3.12 -m pip install -e '.[dev]'
lint:
	ruff check services infra tests scripts research
test:
	$(PYTHON) -m pytest -q
synth:
	CDK_OUTDIR=cdk.out JSII_RUNTIME_PACKAGE_CACHE_ROOT=.cache/jsii $(PYTHON) infra/app.py
diff:
	@echo 'Review docs/aws-setup.md and the synthesized template before any AWS change.'
	cdk diff
deploy:
	@echo 'Deployment requires explicit user approval after reviewing the diff.'
	cdk deploy --all
seed:
	python3 scripts/seed.py
verify:
	python3 scripts/verify_local.py
demo:
	python3 scripts/demo.py
teardown:
	python3 scripts/teardown.py --dry-run
research:
	python3 research/run.py --config research/config.json
