# spendctl

AWS Spend Admission Controller project. This repository starts from an empty checkout; it contains the local policy/research core and deployment scaffolding, not a live AWS deployment. See the module acceptance tests and verification status in `docs/`.

## Commands

Python 3.12 is the target runtime (`PYTHON=python3.12` can select another executable for local diagnostics). `make install` installs the development requirements, `make test` runs local tests, `make research` runs the deterministic synthetic evaluation, and `make synth` synthesizes CDK without deploying. AWS-changing commands are intentionally explicit; read `docs/aws-setup.md` and obtain approval before deployment, EC2 launch, billing configuration, or teardown.

## Contracts

The API and event names are documented in `docs/api.md` and `docs/events.md`. Ledger money is integer micro-USD. See `docs/decisions.md` for the gp3 policy interpretation and adaptive update semantics.
