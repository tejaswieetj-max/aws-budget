import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws
from spendctl.admission import (
    AdmissionConflict,
    AdmissionInsufficientBudget,
    AdmissionOutcomeUnknown,
    AdmissionPricingUnavailable,
    AdmissionRetryableFailure,
    AdmissionService,
    UnauthorizedTeam,
    VerifiedIdentity,
)
from spendctl.ledger import AmbiguousLedgerOutcome, DynamoLedger, LedgerTransactionCancelled
from spendctl.pricing import (
    CachedPricingProvider,
    DynamoDBPriceCache,
    FixturePricingProvider,
    InMemoryPriceCache,
    PriceList,
    PriceNotFound,
    ResilientPricingProvider,
    TieredPriceCache,
    UnsupportedPricingConfiguration,
    estimate,
    estimate_request,
)

PERIOD = "2026-10"
BODY = {
    "team": "alpha",
    "resource": {"type": "ec2", "instance_type": "t3.micro", "ebs_gb": 8},
    "duration_hours": 1,
    "purpose": "test",
}
IDENTITY = VerifiedIdentity(team="alpha", subject="user-1")


class FakePriceClient:
    def __init__(self, pages):
        self.pages = pages
        self.requests = []

    def get_products(self, **kwargs):
        self.requests.append(kwargs)
        return self.pages.pop(0)


def compute_product(instance_type="t3.micro", rate="0.012", **overrides):
    attrs = {
        "location": "Asia Pacific (Mumbai)",
        "instanceType": instance_type,
        "operatingSystem": "Linux",
        "tenancy": "Shared",
        "preInstalledSw": "NA",
        "capacitystatus": "Used",
        **overrides,
    }
    return {
        "product": {"productFamily": "Compute Instance", "attributes": attrs},
        "terms": {
            "OnDemand": {
                "term": {
                    "priceDimensions": {"dimension": {"unit": "Hrs", "pricePerUnit": {"USD": rate}}}
                }
            }
        },
    }


def test_supported_estimate_is_deterministic_and_separates_components():
    provider = FixturePricingProvider()
    one = estimate_request(BODY, provider)
    two = estimate_request(BODY, provider)
    assert one == two
    assert one["source"] == "fixture_unverified"
    assert one["region"] == "ap-south-1"
    assert one["compute_micro"] == 12_000
    assert one["storage_micro"] == 877
    assert one["total_micro"] == 12_877
    assert one["price_snapshot_id"]


def test_supported_small_instance_and_maximum_runtime():
    payload = {
        **BODY,
        "resource": {"type": "ec2", "instance_type": "t3.small", "ebs_gb": 30},
        "duration_hours": 24,
    }
    result = estimate_request(payload, FixturePricingProvider())
    assert result["configuration"] == {
        "instance_type": "t3.small",
        "ebs_gb": 30,
        "duration_hours": 24,
    }


@pytest.mark.parametrize(
    "change",
    [
        {"resource": {"type": "ec2", "instance_type": "m5.large", "ebs_gb": 8}},
        {"resource": {"type": "ec2", "instance_type": "t3.micro", "ebs_gb": 31}},
        {"resource": {"type": "ec2", "instance_type": "t3.micro", "ebs_gb": 0}},
        {"duration_hours": 25},
    ],
)
def test_rejected_configurations(change):
    payload = {**BODY, **change}
    with pytest.raises(ValueError):
        estimate_request(payload, FixturePricingProvider())


def test_rounding_boundaries_round_each_component_up():
    result = estimate(Decimal("0.0000001"), Decimal("0.0000001"), 1, 1)
    assert result["compute_micro"] == 1
    assert result["storage_micro"] == 1
    assert result["total_micro"] == 2


def test_price_list_paginates_and_validates_product_selection():
    first = {
        "PriceList": [json.dumps(compute_product("t3.small"))],
        "NextToken": "next",
    }
    second = {"PriceList": [json.dumps(compute_product())]}
    client = FakePriceClient([first, second])
    provider = PriceList(client)
    assert provider.instance_hourly_usd("t3.micro") == Decimal("0.012")
    assert len(client.requests) == 2
    assert client.requests[1]["NextToken"] == "next"


def test_price_list_missing_and_unsupported_sku_are_explicit():
    with pytest.raises(PriceNotFound):
        PriceList(FakePriceClient([{"PriceList": []}])).instance_hourly_usd("t3.micro")
    with pytest.raises(UnsupportedPricingConfiguration):
        PriceList(FakePriceClient([])).instance_hourly_usd("m5.large")


def test_price_list_gp3_selects_storage_gb_month_dimension():
    product = {
        "product": {
            "productFamily": "Storage",
            "attributes": {"location": "Asia Pacific (Mumbai)", "volumeApiName": "gp3"},
        },
        "terms": {
            "OnDemand": {
                "term": {
                    "priceDimensions": {
                        "dimension": {
                            "unit": "GB-Mo",
                            "pricePerUnit": {"USD": "0.08"},
                        }
                    }
                }
            }
        },
    }
    assert PriceList(
        FakePriceClient([{"PriceList": [json.dumps(product)]}])
    ).gp3_gb_month_usd() == Decimal("0.08")


def test_cache_hits_and_expires_deterministically():
    now = [10.0]

    class Clock:
        def __call__(self):
            return now[0]

    class CountingProvider(FixturePricingProvider):
        calls = 0

        def instance_hourly_usd(self, instance_type, region="ap-south-1"):
            self.calls += 1
            return super().instance_hourly_usd(instance_type, region)

    base = CountingProvider()
    cached = CachedPricingProvider(base, InMemoryPriceCache(Clock()), ttl_seconds=3)
    cached.instance_hourly_usd("t3.micro")
    cached.instance_hourly_usd("t3.micro")
    assert base.calls == 1
    now[0] += 3
    cached.instance_hourly_usd("t3.micro")
    assert base.calls == 2


def test_provider_failure_is_not_misreported_as_a_price():
    class BrokenProvider(FixturePricingProvider):
        def instance_hourly_usd(self, instance_type, region="ap-south-1"):
            raise PriceNotFound("no price")

    with pytest.raises(AdmissionPricingUnavailable, match="no price"):
        AdmissionService(ledger=object(), pricing=BrokenProvider()).estimate(BODY, IDENTITY)


def test_price_list_sdk_failure_is_explicit_pricing_failure():
    class FailedClient:
        def get_products(self, **kwargs):
            raise ClientError(
                {"Error": {"Code": "InternalError", "Message": "failed"}}, "GetProducts"
            )

    with pytest.raises(AdmissionPricingUnavailable, match="pricing provider request failed"):
        AdmissionService(ledger=object(), pricing=PriceList(FailedClient())).estimate(
            BODY, IDENTITY
        )


def test_price_list_failure_uses_explicitly_labelled_fallback_and_snapshot():
    class FailedClient:
        def get_products(self, **kwargs):
            raise TimeoutError("price service unavailable")

    logs = []
    provider = ResilientPricingProvider(PriceList(FailedClient()), log=logs.append)
    cached = CachedPricingProvider(provider, InMemoryPriceCache())
    first = estimate_request(BODY, cached)
    second = estimate_request(
        BODY,
        CachedPricingProvider(
            ResilientPricingProvider(PriceList(FailedClient()), log=logs.append),
            InMemoryPriceCache(),
        ),
    )
    assert first["source"] == "fallback_unverified"
    assert first["price_snapshot_id"] == second["price_snapshot_id"]
    assert cached.price_fetched_at
    assert logs and "fallback_unverified" in logs[0]


def test_dynamodb_price_cache_persists_fetch_metadata_and_expires():
    with mock_aws():
        db = boto3.client("dynamodb", region_name="us-east-1")
        db.create_table(
            TableName="PriceCache",
            KeySchema=[{"AttributeName": "sku_key", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "sku_key", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        clock = [1_000_000.0]
        cache = DynamoDBPriceCache(db, "PriceCache", clock=lambda: clock[0])
        memory = InMemoryPriceCache(clock=lambda: clock[0])
        layered = TieredPriceCache(memory, cache)
        layered.set("fixture:ap-south-1:ec2:t3.micro", Decimal("0.012"), 10)
        assert layered.get("fixture:ap-south-1:ec2:t3.micro") == Decimal("0.012")
        assert layered.fetched_at("fixture:ap-south-1:ec2:t3.micro")
        clock[0] += 11
        assert layered.get("fixture:ap-south-1:ec2:t3.micro") is None


@pytest.fixture
def ledger():
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="Budgets",
            KeySchema=[
                {"AttributeName": "team", "KeyType": "HASH"},
                {"AttributeName": "period", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "team", "AttributeType": "S"},
                {"AttributeName": "period", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        client.create_table(
            TableName="Reservations",
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
                {"AttributeName": "team", "AttributeType": "S"},
                {"AttributeName": "created_at", "AttributeType": "S"},
                {"AttributeName": "status", "AttributeType": "S"},
                {"AttributeName": "expires_at", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "team-created-index",
                    "KeySchema": [
                        {"AttributeName": "team", "KeyType": "HASH"},
                        {"AttributeName": "created_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": "status-expiry-index",
                    "KeySchema": [
                        {"AttributeName": "status", "KeyType": "HASH"},
                        {"AttributeName": "expires_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        client.create_table(
            TableName="Alerts",
            KeySchema=[{"AttributeName": "alert_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "alert_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        instance = DynamoLedger(client, "Budgets", "Reservations", "Alerts")
        instance.create_budget(team="alpha", period=PERIOD, budget_micro=50_000)
        yield instance


def service(ledger, pricing=None):
    return AdmissionService(ledger, pricing or FixturePricingProvider())


def invariant(ledger):
    budget = ledger.get_budget("alpha", PERIOD)
    assert (
        budget["available_micro"] + budget["reserved_micro"] + budget["spent_micro"]
        == budget["budget_micro"]
    )
    return budget


def test_admission_reserves_atomically_and_replay_is_durable(ledger):
    admission = service(ledger)
    first = admission.admit(BODY, identity=IDENTITY, period=PERIOD, idempotency_key="key-1")
    replay = admission.admit(BODY, identity=IDENTITY, period=PERIOD, idempotency_key="key-1")
    assert first["state"] == "ADMITTED"
    assert first["http_status"] == 202
    assert replay["http_status"] == 200
    assert replay["replayed"] is True
    assert replay["reservation_id"] == first["reservation_id"]
    assert first["provisioning_started"] is False
    stored = ledger.get_reservation(first["reservation_id"])
    assert stored["price_snapshot_id"]
    assert stored["price_fetched_at"]
    assert stored["price_source"] == "fixture_unverified"
    assert invariant(ledger)["reserved_micro"] == first["reserved_micro"]


def test_concurrent_retries_of_same_admission_reserve_once(ledger):
    admission = service(ledger)

    def run(_):
        return admission.admit(
            BODY, identity=IDENTITY, period=PERIOD, idempotency_key="same-concurrent"
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(run, range(8)))
    assert len({result["reservation_id"] for result in outcomes}) == 1
    assert sum(result["reserved_micro"] for result in outcomes) == 8 * outcomes[0]["reserved_micro"]
    budget = invariant(ledger)
    assert budget["reserved_micro"] == outcomes[0]["reserved_micro"]


def test_admission_rejects_cross_team_and_conflicting_key_reuse(ledger):
    admission = service(ledger)
    with pytest.raises(UnauthorizedTeam):
        admission.admit(
            BODY, identity=VerifiedIdentity("beta", "user-2"), period=PERIOD, idempotency_key="x"
        )
    admission.admit(BODY, identity=IDENTITY, period=PERIOD, idempotency_key="same")
    changed = {**BODY, "duration_hours": 2}
    with pytest.raises(AdmissionConflict):
        admission.admit(changed, identity=IDENTITY, period=PERIOD, idempotency_key="same")
    assert invariant(ledger)["reserved_micro"] > 0


def test_insufficient_budget_does_not_change_balances(ledger):
    small_budget = ledger.get_budget("alpha", PERIOD)["budget_micro"]
    ledger.db.update_item(
        TableName="Budgets",
        Key={"team": {"S": "alpha"}, "period": {"S": PERIOD}},
        UpdateExpression="SET budget_micro=:b, available_micro=:b",
        ExpressionAttributeValues={":b": {"N": "10000"}},
    )
    with pytest.raises(AdmissionInsufficientBudget):
        service(ledger).admit(BODY, identity=IDENTITY, period=PERIOD, idempotency_key="too-large")
    assert invariant(ledger)["reserved_micro"] == 0
    assert small_budget == 50_000


def test_concurrent_admissions_preserve_budget_invariant(ledger):
    admission = service(ledger)

    def run(index):
        try:
            return admission.admit(
                BODY, identity=IDENTITY, period=PERIOD, idempotency_key=f"concurrent-{index}"
            )["state"]
        except AdmissionInsufficientBudget:
            return "INSUFFICIENT"

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(run, range(8)))
    assert outcomes.count("ADMITTED") == 3
    assert outcomes.count("INSUFFICIENT") == 5
    budget = invariant(ledger)
    assert budget["reserved_micro"] <= budget["budget_micro"]


def test_ambiguous_reserve_is_unknown_and_never_released_or_started(ledger):
    class AmbiguousOnce:
        def __init__(self, inner):
            self.inner = inner
            self.did_reserve = False

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def reserve(self, **kwargs):
            self.inner.reserve(**kwargs)
            self.did_reserve = True
            raise AmbiguousLedgerOutcome("lost response after commit")

    wrapped = AmbiguousOnce(ledger)
    result = service(wrapped).admit(
        BODY, identity=IDENTITY, period=PERIOD, idempotency_key="ambiguous"
    )
    assert result["replayed"] is True
    assert result["provisioning_started"] is False
    assert wrapped.did_reserve
    assert invariant(ledger)["reserved_micro"] == result["reserved_micro"]


def test_unresolved_ambiguous_outcome_returns_safe_pending(ledger):
    class UnknownLedger:
        def __init__(self, inner):
            self.inner = inner
            self.reserve_calls = 0

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def reserve(self, **kwargs):
            self.reserve_calls += 1
            raise AmbiguousLedgerOutcome("connection lost")

        def get_reservation(self, reservation_id):
            raise AmbiguousLedgerOutcome("read failed")

    wrapped = UnknownLedger(ledger)
    admission = service(wrapped)
    with pytest.raises(AdmissionOutcomeUnknown) as error:
        admission.admit(BODY, identity=IDENTITY, period=PERIOD, idempotency_key="unknown")
    assert error.value.reservation_id
    assert error.value.state == "PENDING_UNKNOWN"
    assert error.value.reconciliation_required is True
    assert wrapped.reserve_calls == 0
    assert invariant(ledger)["reserved_micro"] == 0


def test_known_transaction_cancellation_is_reported_as_retryable(ledger):
    class CancelledLedger:
        def __getattr__(self, name):
            return getattr(ledger, name)

        def reserve(self, **kwargs):
            raise LedgerTransactionCancelled("conditional write did not commit")

    with pytest.raises(AdmissionRetryableFailure, match="cancelled"):
        service(CancelledLedger()).admit(
            BODY, identity=IDENTITY, period=PERIOD, idempotency_key="cancelled"
        )
    assert invariant(ledger)["reserved_micro"] == 0


def test_reconciliation_uses_same_key_and_returns_confirmed_reservation(ledger):
    admission = service(ledger)
    first = admission.admit(BODY, identity=IDENTITY, period=PERIOD, idempotency_key="reconcile")
    resolved = admission.reconcile(
        BODY, identity=IDENTITY, period=PERIOD, idempotency_key="reconcile"
    )
    assert resolved["reservation_id"] == first["reservation_id"]
    assert resolved["replayed"] is True
