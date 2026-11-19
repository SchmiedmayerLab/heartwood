# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""The synthetic price sheet and the estimates that the control service prices from it.

Each price is a row of the FOCUS SKU Price dataset, with the column names of the
draft revision that the platform contract pins. Which workload a price applies to is
kept apart from the FOCUS columns: a runtime profile names the kind of work it runs
and the price of each measure of that work.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator

from heartwood.schemas import (
    CostDenomination,
    ElementEstimate,
    EstimatedCost,
    FocusEvidenceReference,
    PlatformEstimate,
    WorkloadElement,
    WorkloadPrediction,
)

# The SKU Price proposal revision that the platform contract pins.
SKU_PRICE_REVISION = "005a30566a754db4e0f346d20c7063daba579080"

Kind = Literal["exploration", "batch", "inference"]
Measure = Literal["duration", "task_duration", "input_tokens", "output_tokens"]
Text = Annotated[str, StringConstraints(min_length=1, max_length=512, pattern=r"\S")]
Identifier = Annotated[str, StringConstraints(pattern=r"^\S{1,512}$")]
DecimalText = Annotated[str, StringConstraints(pattern=r"^(0|[1-9][0-9]*)(\.[0-9]+)?$")]

# The measures that price each kind of work.
MEASURES: dict[str, frozenset[str]] = {
    "exploration": frozenset({"duration"}),
    "batch": frozenset({"task_duration"}),
    "inference": frozenset({"input_tokens", "output_tokens"}),
}
_AMOUNT = Decimal("0.000001")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class GlobalEligibility(_Model):
    """A SKU Price Eligibility object that applies a price to every project."""

    is_global_scope: Literal[True] = Field(alias="IsGlobalScope")


class SkuPrice(_Model):
    """One row of the FOCUS SKU Price dataset."""

    service_provider_name: Text = Field(alias="ServiceProviderName")
    sku_id: Identifier = Field(alias="SkuId")
    sku_price_id: Identifier = Field(alias="SkuPriceId")
    contract_id: Identifier | None = Field(default=None, alias="ContractId")
    charge_category: Literal["Usage"] = Field(alias="ChargeCategory")
    unit_price: DecimalText = Field(alias="UnitPrice")
    unit_price_type: Literal["List", "Base", "Contracted"] = Field(alias="UnitPriceType")
    pricing_unit: Text = Field(alias="PricingUnit")
    pricing_currency: Identifier = Field(alias="PricingCurrency")
    pricing_currency_category: Literal["Payable", "Consumable"] = Field(
        alias="PricingCurrencyCategory"
    )
    effective_start: AwareDatetime = Field(alias="SkuPriceEffectiveStart")
    effective_end: AwareDatetime | None = Field(default=None, alias="SkuPriceEffectiveEnd")
    eligibility: GlobalEligibility = Field(alias="SkuPriceEligibility")

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if (self.unit_price_type == "List") != (self.contract_id is None):
            raise ValueError("a List price has no ContractId, and a contract price has one")
        if self.effective_end is not None and self.effective_end <= self.effective_start:
            raise ValueError("SkuPriceEffectiveEnd must follow SkuPriceEffectiveStart")
        payable = self.pricing_currency_category == "Payable"
        if payable and not (len(self.pricing_currency) == 3 and self.pricing_currency.isupper()):
            raise ValueError("a Payable PricingCurrency is an ISO 4217 code")
        return self

    def effective(self, at: datetime) -> bool:
        """Return whether the price is in effect at a time."""
        return self.effective_start <= at and (
            self.effective_end is None or at < self.effective_end
        )


class Rate(_Model):
    """The price of one measure of a runtime profile's work."""

    measure: Measure
    sku_price_id: Identifier
    # How much of the measure, in its unit, one PricingUnit holds, such as 1000000
    # tokens for the PricingUnit "1,000,000 Tokens".
    quantity_per_pricing_unit: DecimalText

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if Decimal(self.quantity_per_pricing_unit) == 0:
            raise ValueError("quantity_per_pricing_unit must be positive")
        return self


class RuntimeProfile(_Model):
    """The kind of work a runtime profile runs and the prices of its measures."""

    runtime_profile_ref: Identifier
    kind: Kind
    rates: tuple[Rate, ...]

    @model_validator(mode="after")
    def _validate(self) -> Self:
        measures = [rate.measure for rate in self.rates]
        if len(set(measures)) != len(measures) or set(measures) != MEASURES[self.kind]:
            expected = ", ".join(sorted(MEASURES[self.kind]))
            raise ValueError(f"{self.kind} work has one rate for each of: {expected}")
        return self


class PriceSheet(_Model):
    """The prices of the platform and the runtime profiles that they apply to."""

    estimate_validity_seconds: int = Field(gt=0, le=7 * 24 * 3600)
    sku_prices: tuple[SkuPrice, ...] = Field(min_length=1)
    runtime_profiles: tuple[RuntimeProfile, ...]

    @model_validator(mode="after")
    def _validate(self) -> Self:
        prices = [price.sku_price_id for price in self.sku_prices]
        if len(set(prices)) != len(prices):
            raise ValueError("SkuPriceId values must be unique")
        profiles = [profile.runtime_profile_ref for profile in self.runtime_profiles]
        if len(set(profiles)) != len(profiles):
            raise ValueError("runtime profiles must be unique")
        for profile in self.runtime_profiles:
            for rate in profile.rates:
                if rate.sku_price_id not in prices:
                    raise ValueError(f"rate names the unknown SkuPriceId {rate.sku_price_id}")
        # One denomination keeps every estimate total in one unit.
        currencies = {(p.pricing_currency, p.pricing_currency_category) for p in self.sku_prices}
        if len(currencies) != 1:
            raise ValueError("every price uses the same PricingCurrency")
        return self

    @property
    def denomination(self) -> CostDenomination:
        """Return the denomination of every price."""
        price = self.sku_prices[0]
        if price.pricing_currency_category == "Payable":
            return CostDenomination(kind="currency", unit=price.pricing_currency)
        return CostDenomination(kind="allocation-unit", unit=price.pricing_currency)

    def price(self, sku_price_id: str) -> SkuPrice:
        """Return the price with an identifier."""
        return next(price for price in self.sku_prices if price.sku_price_id == sku_price_id)

    def profile(self, runtime_profile_ref: str) -> RuntimeProfile | None:
        """Return the runtime profile with a reference, if the sheet prices it."""
        return next(
            (p for p in self.runtime_profiles if p.runtime_profile_ref == runtime_profile_ref),
            None,
        )


class EstimateRequestError(ValueError):
    """The estimate request names elements inconsistently with its scope."""


# The lower, expected and upper value of a quantity or an amount.
_Bounds = tuple[Decimal, Decimal, Decimal]


# The quantities of each kind of work, in their units, and whether a price needs them.
_QUANTITIES: dict[str, dict[str, tuple[str, bool]]] = {
    "exploration": {"duration": ("seconds", True)},
    "batch": {
        "tasks": ("count", True),
        "duration_per_task": ("seconds", True),
        "retry_allowance": ("count", False),
    },
    "inference": {"input_tokens": ("tokens", True), "output_tokens": ("tokens", True)},
}


def _measures(element: WorkloadElement) -> dict[str, _Bounds] | str:
    """Return the measured work of an element, or the reason it cannot be measured."""
    wanted = _QUANTITIES[element.kind]
    found: dict[str, _Bounds] = {}
    for item in element.quantities:
        if item.metric not in wanted:
            continue
        unit = wanted[item.metric][0]
        if item.unit != unit:
            return f"{item.metric} is given in {item.unit}; the price sheet expects {unit}."
        if item.expected is None:
            return f"The prediction gives no expected value for {item.metric}."
        expected = Decimal(item.expected)
        lower = expected if item.lower is None else Decimal(item.lower)
        upper = expected if item.upper is None else Decimal(item.upper)
        found[item.metric] = (lower, expected, upper)
    missing = [metric for metric, (_, needed) in wanted.items() if needed and metric not in found]
    if missing:
        return f"The prediction gives no {', '.join(missing)}."
    if element.kind != "batch":
        return found
    tasks, duration = found["tasks"], found["duration_per_task"]
    retries = found.get("retry_allowance", (Decimal(0),) * 3)
    lower, expected, upper = ((tasks[i] + retries[i]) * duration[i] for i in range(3))
    return {"task_duration": (lower, expected, upper)}


def _amount(value: Decimal) -> str:
    rounded = value.quantize(_AMOUNT, rounding=ROUND_HALF_EVEN)
    return "0" if rounded == 0 else format(rounded.normalize(), "f")


@dataclass(frozen=True)
class _Priced:
    """The cost of one element and the prices it used."""

    cost: EstimatedCost
    prices: tuple[SkuPrice, ...] = ()


def _price_element(sheet: PriceSheet, element: WorkloadElement, now: datetime) -> _Priced:
    denomination = sheet.denomination

    def unknown(limitation: str) -> _Priced:
        cost = EstimatedCost(
            denomination=denomination, coverage="unknown", limitations=(limitation,)
        )
        return _Priced(cost)

    profile = sheet.profile(element.runtime_profile_ref)
    if profile is None:
        return unknown(f"Runtime profile {element.runtime_profile_ref} has no price.")
    if profile.kind != element.kind:
        return unknown(
            f"Runtime profile {profile.runtime_profile_ref} prices only {profile.kind} work."
        )
    measures = _measures(element)
    if isinstance(measures, str):
        return unknown(measures)
    totals = [Decimal(0)] * 3
    prices: list[SkuPrice] = []
    for rate in profile.rates:
        price = sheet.price(rate.sku_price_id)
        if not price.effective(now):
            return unknown(f"SKU price {price.sku_price_id} is not in effect.")
        prices.append(price)
        factor = Decimal(price.unit_price) / Decimal(rate.quantity_per_pricing_unit)
        for index, value in enumerate(measures[rate.measure]):
            totals[index] += value * factor
    lower, expected, upper = (_amount(value) for value in totals)
    cost = EstimatedCost(
        denomination=denomination,
        coverage="complete",
        expected=expected,
        lower=None if lower == expected else lower,
        upper=None if upper == expected else upper,
    )
    return _Priced(cost, tuple(prices))


def _total(denomination: CostDenomination, costs: Sequence[EstimatedCost]) -> EstimatedCost:
    known = [cost for cost in costs if cost.coverage != "unknown"]
    limitations = tuple(dict.fromkeys(text for cost in costs for text in cost.limitations))
    if not known:
        return EstimatedCost(denomination=denomination, coverage="unknown", limitations=limitations)

    def summed(bound: Literal["lower", "expected", "upper"]) -> str:
        values = [str(getattr(cost, bound) or cost.expected) for cost in known]
        return _amount(sum((Decimal(value) for value in values), Decimal(0)))

    lower, expected, upper = summed("lower"), summed("expected"), summed("upper")
    return EstimatedCost(
        denomination=denomination,
        coverage="complete" if len(known) == len(costs) else "partial",
        expected=expected,
        lower=None if lower == expected else lower,
        upper=None if upper == expected else upper,
        limitations=limitations,
    )


def _evidence(price: SkuPrice) -> FocusEvidenceReference:
    row = json.dumps(price.model_dump(mode="json", by_alias=True), sort_keys=True)
    return FocusEvidenceReference(
        specification_version="1.5-draft",
        dataset="SKU Price",
        reference=price.sku_price_id,
        draft_revision=SKU_PRICE_REVISION,
        content_sha256=hashlib.sha256(row.encode()).hexdigest(),
    )


def estimate(
    sheet: PriceSheet,
    prediction: WorkloadPrediction,
    *,
    scope: Literal["plan", "elements"],
    element_ids: Sequence[str] | None,
    now: datetime,
) -> PlatformEstimate:
    """Price a prediction for the whole plan or for named elements.

    An element whose price or quantities are missing has an unknown cost with the
    reason, and the total then covers the priced elements only.
    """
    by_id = {element.element_id: element for element in prediction.elements}
    if scope == "plan":
        if element_ids is not None:
            raise EstimateRequestError("a plan estimate names no elements")
        selected = tuple(by_id)
    else:
        if not element_ids or len(set(element_ids)) != len(element_ids):
            raise EstimateRequestError("an element estimate names distinct elements")
        if not set(element_ids) <= set(by_id):
            raise EstimateRequestError("an element estimate names elements of the prediction")
        selected = tuple(element_ids)
    priced = [_price_element(sheet, by_id[element_id], now) for element_id in selected]
    used = {price.sku_price_id: price for item in priced for price in item.prices}
    assumptions = [
        "Prices come from the synthetic price sheet of the reference platform.",
        "Amounts are rounded to six decimal places.",
    ]
    if any(
        by_id[element_id].kind == "batch"
        and all(item.metric != "retry_allowance" for item in by_id[element_id].quantities)
        for element_id in selected
    ):
        assumptions.append("Batch work without a retry allowance is priced without retries.")
    ends = [price.effective_end for price in used.values() if price.effective_end is not None]
    return PlatformEstimate(
        project_context_id=prediction.project_context_id,
        estimate_id=f"est-{uuid.uuid4().hex}",
        scope=scope,
        prediction=prediction.prediction,
        plan=prediction.plan,
        created_at=now,
        valid_until=min([now + timedelta(seconds=sheet.estimate_validity_seconds), *ends]),
        total=_total(sheet.denomination, [item.cost for item in priced]),
        covered_element_ids=selected,
        element_estimates=tuple(
            ElementEstimate(element_ids=(element_id,), cost=item.cost, included_in_total=True)
            for element_id, item in zip(selected, priced, strict=True)
        ),
        pricing_assumptions=tuple(assumptions),
        focus_evidence=tuple(_evidence(used[key]) for key in sorted(used)),
    )
