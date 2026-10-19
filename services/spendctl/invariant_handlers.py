"""M4 invariant-checker Lambda adapter."""

from __future__ import annotations

import os

import boto3

from .invariants import InvariantChecker


def invariant_checker_handler(event, context):
    region = os.environ.get("AWS_REGION", "ap-south-1")
    checker = InvariantChecker(
        boto3.client("dynamodb", region_name=region),
        budgets_table=os.environ["BUDGETS_TABLE"],
        reservations_table=os.environ["RESERVATIONS_TABLE"],
        alerts_table=os.environ["ALERTS_TABLE"],
        eventbridge=boto3.client("events", region_name=region),
        event_bus_name=os.environ["EVENT_BUS_NAME"],
    )
    return checker.check()
