"""Deterministic EC2/EBS estimates with explicit pricing-provider provenance."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal
from typing import Protocol

from botocore.exceptions import BotoCoreError, ClientError

from .money import MICRO_USD
from .observability import emit_emf, structured_log
from .validation import Request, validate_request

PRICE_REGION = "us-east-1"  # AWS Price List API endpoint region, not the priced region.
AWS_REGION = "ap-south-1"
LOCATION = "Asia Pacific (Mumbai)"
HOURS_PER_MONTH = Decimal(730)
SUPPORTED_INSTANCE_TYPES = frozenset({"t3.micro", "t3.small"})


class PricingError(Exception):
    """Base class for known pricing lookup and estimation failures."""


class UnsupportedPricingConfiguration(PricingError):
    pass


class PriceNotFound(PricingError):
    pass


class InvalidPriceResponse(PricingError):
    pass


class PriceProviderFailure(PricingError):
    pass


class PricingProvider(Protocol):
    """Returns USD unit rates as Decimal and labels where those rates came from."""

    @property
    def source(self) -> str: ...

    def instance_hourly_usd(self, instance_type: str, region: str = AWS_REGION) -> Decimal: ...

    def gp3_gb_month_usd(self, region: str = AWS_REGION) -> Decimal: ...


class PriceCache(Protocol):
    """Minimal cache interface for provider unit prices."""

    def get(self, key: str) -> Decimal | None: ...

    def set(self, key: str, value: Decimal, ttl_seconds: int) -> None: ...


class DynamoDBPriceCache:
    """Seven-day persistent SKU cache with explicit fetch provenance."""

    def __init__(self, client, table_name: str, *, clock: Callable[[], float] = time.time):
        self.client = client
        self.table_name = table_name
        self.clock = clock
        self._fetched_at: dict[str, str] = {}

    def get(self, key: str) -> Decimal | None:
        item = self.client.get_item(
            TableName=self.table_name, Key={"sku_key": {"S": key}}, ConsistentRead=True
        ).get("Item")
        if not item or int(item.get("ttl", {}).get("N", "0")) <= int(self.clock()):
            return None
        fetched = item.get("fetched_at", {}).get("S")
        if fetched:
            self._fetched_at[key] = fetched
        return Decimal(item["rate_usd"]["N"])

    def set(self, key: str, value: Decimal, ttl_seconds: int) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        fetched = datetime.fromtimestamp(self.clock(), UTC).isoformat().replace("+00:00", "Z")
        self._fetched_at[key] = fetched
        self.client.put_item(
            TableName=self.table_name,
            Item={
                "sku_key": {"S": key},
                "rate_usd": {"N": str(value)},
                "fetched_at": {"S": fetched},
                "ttl": {"N": str(int(self.clock()) + ttl_seconds)},
            },
        )

    def fetched_at(self, key: str) -> str | None:
        return self._fetched_at.get(key)


class TieredPriceCache:
    """Keep hot rates in process memory and refill them from the shared DynamoDB cache."""

    def __init__(self, memory: PriceCache, persistent: PriceCache):
        self.memory = memory
        self.persistent = persistent

    def get(self, key: str) -> Decimal | None:
        value = self.memory.get(key)
        if value is not None:
            return value
        value = self.persistent.get(key)
        if value is not None:
            self.memory.set(key, value, 7 * 86400)
        return value

    def set(self, key: str, value: Decimal, ttl_seconds: int) -> None:
        self.memory.set(key, value, ttl_seconds)
        self.persistent.set(key, value, ttl_seconds)

    def fetched_at(self, key: str) -> str | None:
        getter = getattr(self.persistent, "fetched_at", None)
        return getter(key) if getter else None


class InMemoryPriceCache:
    """Process-local TTL cache, suitable for tests and single-process tools."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._values: dict[str, tuple[Decimal, float]] = {}

    def get(self, key: str) -> Decimal | None:
        entry = self._values.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if self._clock() >= expires_at:
            self._values.pop(key, None)
            return None
        return value

    def set(self, key: str, value: Decimal, ttl_seconds: int) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._values[key] = (value, self._clock() + ttl_seconds)


class CachedPricingProvider:
    """Caches unit rates while preserving the wrapped provider's provenance."""

    def __init__(self, provider: PricingProvider, cache: PriceCache, ttl_seconds: int = 7 * 86400):
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self.provider = provider
        self.cache = cache
        self.ttl_seconds = ttl_seconds
        self.price_fetched_at: str | None = None

    @property
    def source(self) -> str:
        return self.provider.source

    def instance_hourly_usd(self, instance_type: str, region: str = AWS_REGION) -> Decimal:
        key = f"{self.source}:{region}:ec2:{instance_type}:linux-shared-ondemand"
        value = self.cache.get(key)
        if value is None:
            emit_emf("Spendctl/Pricing", {"PriceCacheMisses": (1, "Count")}, dimensions={"result": "cache_miss"})
            value = _validated_price(self.provider.instance_hourly_usd(instance_type, region), key)
            key = f"{self.source}:{region}:ec2:{instance_type}:linux-shared-ondemand"
            self.cache.set(key, value, self.ttl_seconds)
        else:
            emit_emf("Spendctl/Pricing", {"PriceCacheHits": (1, "Count")}, dimensions={"result": "cache_hit"})
        self.price_fetched_at = _cache_timestamp(self.cache, key)
        return value

    def gp3_gb_month_usd(self, region: str = AWS_REGION) -> Decimal:
        key = f"{self.source}:{region}:ebs:gp3:gb-month"
        value = self.cache.get(key)
        if value is None:
            emit_emf("Spendctl/Pricing", {"PriceCacheMisses": (1, "Count")}, dimensions={"result": "cache_miss"})
            value = _validated_price(self.provider.gp3_gb_month_usd(region), key)
            key = f"{self.source}:{region}:ebs:gp3:gb-month"
            self.cache.set(key, value, self.ttl_seconds)
        else:
            emit_emf("Spendctl/Pricing", {"PriceCacheHits": (1, "Count")}, dimensions={"result": "cache_hit"})
        self.price_fetched_at = _cache_timestamp(self.cache, key)
        return value


FALLBACK_PRICES = {"t3.micro": Decimal("0.012"), "t3.small": Decimal("0.024")}


class ResilientPricingProvider:
    """Fallback rates are explicitly labelled and logged after a provider failure."""

    def __init__(self, primary: PricingProvider, *, log=print):
        self.primary = primary
        self.log = log
        self.fallback_active = False

    @property
    def source(self) -> str:
        return "fallback_unverified" if self.fallback_active else self.primary.source

    def _fallback(self, error) -> None:
        self.fallback_active = True
        emit_emf("Spendctl/Pricing", {"FallbackPriceUsage": (1, "Count")}, dimensions={"result": "fallback"})
        emit_emf("Spendctl/Pricing", {"PriceLookupFailures": (1, "Count")}, dimensions={"result": "failure"})
        structured_log("M3", "price_lookup", "fallback", error_category=type(error).__name__)
        self.log(
            json.dumps(
                {
                    "event": "PricingFallbackSelected",
                    "source": self.source,
                    "reason": type(error).__name__,
                },
                sort_keys=True,
            )
        )

    def instance_hourly_usd(self, instance_type: str, region: str = AWS_REGION) -> Decimal:
        _validate_sku(instance_type, region)
        if self.fallback_active:
            return FALLBACK_PRICES[instance_type]
        try:
            return self.primary.instance_hourly_usd(instance_type, region)
        except (
            PricingError,
            BotoCoreError,
            ClientError,
            TimeoutError,
            ConnectionError,
            OSError,
        ) as error:
            self._fallback(error)
            return FALLBACK_PRICES[instance_type]

    def gp3_gb_month_usd(self, region: str = AWS_REGION) -> Decimal:
        _validate_region(region)
        if self.fallback_active:
            return Decimal("0.08")
        try:
            return self.primary.gp3_gb_month_usd(region)
        except (
            PricingError,
            BotoCoreError,
            ClientError,
            TimeoutError,
            ConnectionError,
            OSError,
        ) as error:
            self._fallback(error)
            return Decimal("0.08")


class FixturePricingProvider:
    """Deterministic, clearly non-live unit prices for tests and offline demos."""

    source = "fixture_unverified"

    def __init__(
        self,
        instance_rates: dict[str, Decimal | str] | None = None,
        gp3_rate: Decimal | str = "0.08",
    ):
        defaults = {"t3.micro": "0.012", "t3.small": "0.024"}
        self.instance_rates = {
            name: Decimal(str(rate)) for name, rate in (instance_rates or defaults).items()
        }
        self.gp3_rate = Decimal(str(gp3_rate))

    def instance_hourly_usd(self, instance_type: str, region: str = AWS_REGION) -> Decimal:
        _validate_sku(instance_type, region)
        try:
            return _validated_price(self.instance_rates[instance_type], instance_type)
        except KeyError as error:
            raise PriceNotFound(f"fixture price missing for {instance_type}") from error

    def gp3_gb_month_usd(self, region: str = AWS_REGION) -> Decimal:
        _validate_region(region)
        return _validated_price(self.gp3_rate, "gp3")


class PriceList:
    """AWS Price List API provider with validated products and full pagination.

    The client must be configured for the Price List API endpoint (normally
    ``us-east-1``). The returned rates are for Mumbai/ap-south-1 Linux shared
    On-Demand compute and gp3 storage; they are not EC2 runtime calls.
    """

    source = "aws_pricelist_unverified"

    def __init__(self, client):
        self.client = client

    def instance_hourly_usd(self, instance_type: str, region: str = AWS_REGION) -> Decimal:
        _validate_sku(instance_type, region)
        filters = _filters(
            {
                "location": LOCATION,
                "operatingSystem": "Linux",
                "tenancy": "Shared",
                "preInstalledSw": "NA",
                "capacitystatus": "Used",
                "instanceType": instance_type,
            }
        )
        rates = set()
        for product in self._products(filters):
            attrs = product.get("product", {}).get("attributes", {})
            if not _valid_compute_product(attrs, instance_type):
                continue
            rates.update(_dimension_rates(product, {"Hrs", "Hours"}))
        return _one_rate(rates, f"{instance_type} Mumbai Linux shared On-Demand hourly")

    def gp3_gb_month_usd(self, region: str = AWS_REGION) -> Decimal:
        _validate_region(region)
        filters = _filters(
            {"location": LOCATION, "productFamily": "Storage", "volumeApiName": "gp3"}
        )
        rates = set()
        for product in self._products(filters):
            attrs = product.get("product", {}).get("attributes", {})
            if (
                product.get("product", {}).get("productFamily") == "Storage"
                and attrs.get("location") == LOCATION
                and attrs.get("volumeApiName") == "gp3"
            ):
                rates.update(_dimension_rates(product, {"GB-Mo", "GB-Month"}))
        return _one_rate(rates, "gp3 Mumbai GB-month")

    def _products(self, filters: list[dict]):
        token = None
        seen_tokens = set()
        while True:
            request = {
                "ServiceCode": "AmazonEC2",
                "Filters": filters,
                "FormatVersion": "aws_v1",
                "MaxResults": 100,
            }
            if token:
                request["NextToken"] = token
            response = self.client.get_products(**request)
            for raw in response.get("PriceList", []):
                try:
                    yield json.loads(raw)
                except (TypeError, json.JSONDecodeError) as error:
                    raise InvalidPriceResponse(
                        "Price List returned malformed product JSON"
                    ) from error
            token = response.get("NextToken")
            if not token:
                break
            if token in seen_tokens:
                raise InvalidPriceResponse("Price List repeated a pagination token")
            seen_tokens.add(token)


def estimate_request(payload: dict, provider: PricingProvider) -> dict:
    """Validate and estimate one v1 request using explicitly identified rates."""
    request = validate_request(payload)
    if not hasattr(provider, "instance_hourly_usd") or not hasattr(provider, "gp3_gb_month_usd"):
        raise TypeError("provider must implement PricingProvider")
    try:
        compute_rate = _validated_price(
            provider.instance_hourly_usd(request.instance_type, AWS_REGION), request.instance_type
        )
        storage_rate = _validated_price(provider.gp3_gb_month_usd(AWS_REGION), "gp3")
    except PricingError:
        raise
    except (TimeoutError, ConnectionError, OSError) as error:
        raise PriceProviderFailure("pricing provider could not be reached") from error
    except (BotoCoreError, ClientError) as error:
        raise PriceProviderFailure("pricing provider request failed") from error
    except (ValueError, ArithmeticError) as error:
        raise InvalidPriceResponse("pricing provider returned invalid unit rates") from error
    return estimate(
        compute_rate,
        storage_rate,
        request.ebs_gb,
        request.duration_hours,
        source=provider.source,
        request=request,
    )


def estimate(
    instance_hourly_usd: Decimal,
    gp3_gb_month_usd: Decimal,
    ebs_gb: int,
    hours: int,
    *,
    source: str = "unspecified",
    request: Request | None = None,
) -> dict:
    """Price compute and gp3 storage separately and round each up to micro-USD."""
    instance_hourly_usd = _validated_price(instance_hourly_usd, "instance hourly rate")
    gp3_gb_month_usd = _validated_price(gp3_gb_month_usd, "gp3 GB-month rate")
    _validate_price_inputs(instance_hourly_usd, gp3_gb_month_usd, ebs_gb, hours)
    compute = instance_hourly_usd * hours
    storage = gp3_gb_month_usd * ebs_gb * Decimal(hours) / HOURS_PER_MONTH
    compute_micro = _usd_decimal_to_micro(compute)
    storage_micro = _usd_decimal_to_micro(storage)
    snapshot_material = {
        "region": AWS_REGION,
        "instance_hourly_usd": str(instance_hourly_usd),
        "gp3_gb_month_usd": str(gp3_gb_month_usd),
        "hours": hours,
        "ebs_gb": ebs_gb,
    }
    if request:
        snapshot_material["instance_type"] = request.instance_type
    snapshot = hashlib.sha256(
        json.dumps(snapshot_material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    result = {
        "compute_micro": compute_micro,
        "storage_micro": storage_micro,
        "total_micro": compute_micro + storage_micro,
        "breakdown": {
            "compute_usd_per_hour": str(instance_hourly_usd),
            "gp3_usd_per_gb_month": str(gp3_gb_month_usd),
            "hours_per_month": int(HOURS_PER_MONTH),
            "compute_micro": compute_micro,
            "storage_micro": storage_micro,
        },
        "region": AWS_REGION,
        "price_snapshot_id": snapshot,
        "source": source,
    }
    if request:
        result["configuration"] = {
            "instance_type": request.instance_type,
            "ebs_gb": request.ebs_gb,
            "duration_hours": request.duration_hours,
        }
    return result


def demo_estimate(instance_type: str, ebs_gb: int, hours: int) -> dict:
    """Offline illustration only; rates are not claimed as live AWS prices."""
    provider = FixturePricingProvider()
    if instance_type not in SUPPORTED_INSTANCE_TYPES:
        raise UnsupportedPricingConfiguration(f"unsupported instance type: {instance_type}")
    return estimate(
        provider.instance_hourly_usd(instance_type),
        provider.gp3_gb_month_usd(),
        ebs_gb,
        hours,
        source=provider.source,
    )


def _validate_sku(instance_type: str, region: str) -> None:
    _validate_region(region)
    if instance_type not in SUPPORTED_INSTANCE_TYPES:
        raise UnsupportedPricingConfiguration(f"unsupported instance type: {instance_type}")


def _validate_region(region: str) -> None:
    if region != AWS_REGION:
        raise UnsupportedPricingConfiguration(f"unsupported AWS region: {region}")


def _validate_price_inputs(instance_rate, storage_rate, ebs_gb, hours) -> None:
    _validated_price(instance_rate, "instance hourly rate")
    _validated_price(storage_rate, "gp3 GB-month rate")
    if type(ebs_gb) is not int or not 1 <= ebs_gb <= 30:
        raise UnsupportedPricingConfiguration("gp3 volume size must be an integer from 1 to 30 GB")
    if type(hours) is not int or not 1 <= hours <= 24:
        raise UnsupportedPricingConfiguration("runtime must be an integer from 1 to 24 hours")


def _validated_price(value, name: str) -> Decimal:
    try:
        price = value if isinstance(value, Decimal) else Decimal(str(value))
    except (ValueError, ArithmeticError) as error:
        raise InvalidPriceResponse(f"invalid price for {name}") from error
    if not price.is_finite() or price < 0:
        raise InvalidPriceResponse(f"price for {name} must be finite and non-negative")
    return price


def _usd_decimal_to_micro(value: Decimal) -> int:
    return int((value * MICRO_USD).quantize(Decimal(1), rounding=ROUND_CEILING))


def _filters(attributes: dict[str, str]) -> list[dict]:
    return [
        {"Type": "TERM_MATCH", "Field": key, "Value": value} for key, value in attributes.items()
    ]


def _valid_compute_product(attrs: dict, instance_type: str) -> bool:
    return (
        attrs.get("location") == LOCATION
        and attrs.get("instanceType") == instance_type
        and attrs.get("operatingSystem") == "Linux"
        and attrs.get("tenancy") == "Shared"
        and attrs.get("preInstalledSw") == "NA"
        and attrs.get("capacitystatus") == "Used"
    )


def _dimension_rates(product: dict, units: set[str]) -> set[Decimal]:
    terms = product.get("terms", {}).get("OnDemand", {})
    rates = set()
    for term in terms.values():
        for dimension in term.get("priceDimensions", {}).values():
            unit = dimension.get("unit")
            price = dimension.get("pricePerUnit", {}).get("USD")
            if unit in units and price is not None:
                rates.add(_validated_price(price, "Price List dimension"))
    return rates


def _one_rate(rates: set[Decimal], description: str) -> Decimal:
    if not rates:
        raise PriceNotFound(f"no supported price found for {description}")
    if len(rates) != 1:
        raise InvalidPriceResponse(f"expected one price for {description}, found {len(rates)}")
    return next(iter(rates))


def _cache_timestamp(cache: PriceCache, key: str) -> str:
    getter = getattr(cache, "fetched_at", None)
    return getter(key) if getter else datetime.now(UTC).isoformat().replace("+00:00", "Z")
