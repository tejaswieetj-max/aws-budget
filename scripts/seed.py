"""Create alpha/beta development budgets after explicit account confirmation."""

from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation

import boto3
from spendctl.ledger import DynamoLedger
from spendctl.money import usd_to_micro


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed development budgets (AWS mutation only with --apply)."
    )
    parser.add_argument("--period", required=True, help="Budget period, for example 2026-10")
    parser.add_argument(
        "--budget-usd", required=True, help="Initial per-team budget in USD; never resets a row"
    )
    parser.add_argument("--region", default="ap-south-1")
    parser.add_argument("--table-prefix", default="spendctl-dev")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--confirm-account", help="Exact 12-digit AWS account ID, required with --apply"
    )
    args = parser.parse_args()
    try:
        budget_micro = usd_to_micro(Decimal(args.budget_usd))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise SystemExit(f"Invalid --budget-usd: {error}") from error
    if budget_micro <= 0:
        raise SystemExit("--budget-usd must be greater than zero")
    if not args.apply:
        print(
            f"Dry run: would create alpha and beta budgets of {budget_micro} micro-USD "
            f"for {args.period} in {args.region}. No AWS API called."
        )
        return
    if (
        not args.confirm_account
        or len(args.confirm_account) != 12
        or not args.confirm_account.isdigit()
    ):
        raise SystemExit("--apply requires --confirm-account with the reviewed 12-digit account ID")

    sts = boto3.client("sts", region_name=args.region)
    account = sts.get_caller_identity()["Account"]
    if account != args.confirm_account:
        raise SystemExit(f"AWS identity account {account} does not match --confirm-account")
    dynamodb = boto3.client("dynamodb", region_name=args.region)
    ledger = DynamoLedger(
        dynamodb,
        f"{args.table_prefix}-budgets",
        f"{args.table_prefix}-reservations",
        f"{args.table_prefix}-alerts",
    )
    for team in ("alpha", "beta"):
        result = ledger.create_budget(team=team, period=args.period, budget_micro=budget_micro)
        print(f"{team}: {'unchanged existing budget' if result['replayed'] else 'created'}")


if __name__ == "__main__":
    main()
