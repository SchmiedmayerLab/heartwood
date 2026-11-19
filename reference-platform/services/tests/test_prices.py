# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fakes import SYNTHETIC_PRICES
from heartwood_platform.control.prices import (
    SKU_PRICE_REVISION,
    EstimateRequestError,
    PriceSheet,
    estimate,
)
from pydantic import ValidationError

from heartwood.schemas import CostDenomination, EstimatedCost, PlatformEstimate, WorkloadPrediction

_FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "synthetic" / "platform-contract"
_NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)


def prediction(revision: int = 1, **changes: Any) -> WorkloadPrediction:  # noqa: ANN401
    """Return a synthetic plan with an exploration, a batch and an inference element."""
    document = json.loads(
        (_FIXTURES / f"research-plan.r{revision}.prediction.json").read_text(encoding="utf-8")
    )
    document.update(changes)
    for evidence in document["evidence"]:
        evidence["project_context_id"] = document["project_context_id"]
    return WorkloadPrediction.model_validate_json(json.dumps(document))


def _sheet(change: Callable[[dict[str, Any]], object] | None = None) -> PriceSheet:
    document = copy.deepcopy(SYNTHETIC_PRICES)
    if change is not None:
        change(document)
    return PriceSheet.model_validate(document)


def _quote(
    sheet: PriceSheet | None = None,
    plan: WorkloadPrediction | None = None,
    element_ids: list[str] | None = None,
    now: datetime = _NOW,
) -> PlatformEstimate:
    return estimate(
        sheet or _sheet(),
        plan or prediction(),
        scope="plan" if element_ids is None else "elements",
        element_ids=element_ids,
        now=now,
    )


def _costs(quote: PlatformEstimate) -> dict[str, EstimatedCost]:
    return {item.element_ids[0]: item.cost for item in quote.element_estimates}


def test_plan_estimate_prices_every_element_from_the_sheet() -> None:
    quote = _quote()

    costs = _costs(quote)
    # 7200 seconds at 0.0005, between 3600 and 10800 seconds.
    assert (costs["exploration"].lower, costs["exploration"].expected) == ("1.8", "3.6")
    assert costs["exploration"].upper == "5.4"
    # 200 tasks and 20 retries of 300 seconds at 0.0003, between 240 and 450 seconds.
    assert (costs["batch"].lower, costs["batch"].expected, costs["batch"].upper) == (
        "15.84",
        "19.8",
        "29.7",
    )
    # 2000000 input tokens at 0.50 and 200000 output tokens at 1.50 per million.
    assert costs["inference"].expected == "1.3"
    assert (costs["inference"].lower, costs["inference"].upper) == ("0.9", "1.95")
    assert quote.total == EstimatedCost(
        denomination=CostDenomination(kind="currency", unit="USD"),
        coverage="complete",
        lower="18.54",
        expected="24.7",
        upper="37.05",
    )
    assert quote.covered_element_ids == ("exploration", "batch", "inference")
    assert all(item.included_in_total for item in quote.element_estimates)
    assert quote.created_at == _NOW
    assert quote.valid_until == _NOW + timedelta(hours=1)
    assert (quote.prediction, quote.plan) == (prediction().prediction, prediction().plan)


def test_gpu_batch_work_is_priced_by_its_runtime_profile() -> None:
    costs = _costs(_quote(plan=prediction(2)))

    # 220 tasks of 20 seconds at 0.004.
    assert costs["batch"].expected == "17.6"


def test_each_quote_has_a_new_identity_and_names_the_prices_it_used() -> None:
    first, second = _quote(), _quote(element_ids=["inference"])

    assert first.estimate_id != _quote().estimate_id
    assert {item.reference for item in first.focus_evidence} == {
        "synthetic-batch-small-list",
        "synthetic-coder-input-list",
        "synthetic-coder-output-list",
        "synthetic-exploration-small-list",
    }
    assert {item.reference for item in second.focus_evidence} == {
        "synthetic-coder-input-list",
        "synthetic-coder-output-list",
    }
    for item in first.focus_evidence:
        assert (item.dataset, item.draft_revision) == ("SKU Price", SKU_PRICE_REVISION)
        assert item.content_sha256 is not None


def test_element_estimate_covers_only_the_named_elements() -> None:
    quote = _quote(element_ids=["batch", "exploration"])

    assert quote.scope == "elements"
    assert quote.covered_element_ids == ("batch", "exploration")
    assert quote.total.expected == "23.4"


def _without_profile(reference: str) -> Callable[[dict[str, Any]], object]:
    def change(document: dict[str, Any]) -> None:
        document["runtime_profiles"] = [
            item
            for item in document["runtime_profiles"]
            if item["runtime_profile_ref"] != reference
        ]

    return change


def test_work_without_a_price_is_unknown_and_the_total_partial() -> None:
    quote = _quote(sheet=_sheet(_without_profile("synthetic-batch-small")))

    batch = _costs(quote)["batch"]
    assert batch.coverage == "unknown"
    assert (batch.lower, batch.expected, batch.upper) == (None, None, None)
    assert batch.limitations == ("Runtime profile synthetic-batch-small has no price.",)
    assert quote.total.coverage == "partial"
    assert quote.total.expected == "4.9"
    assert quote.total.limitations == batch.limitations
    assert quote.covered_element_ids == ("exploration", "batch", "inference")


def test_estimate_without_any_price_has_no_amount() -> None:
    quote = _quote(sheet=_sheet(_without_profile("synthetic-coder")), element_ids=["inference"])

    assert quote.total.coverage == "unknown"
    assert quote.total.expected is None
    assert quote.focus_evidence == ()


def _element(plan: WorkloadPrediction, element_id: str, **changes: Any) -> WorkloadPrediction:  # noqa: ANN401
    document = plan.model_dump(mode="json")
    for element in document["elements"]:
        if element["element_id"] == element_id:
            element.update(changes)
    return WorkloadPrediction.model_validate_json(json.dumps(document))


@pytest.mark.parametrize(
    ("changes", "limitation"),
    [
        (
            {"runtime_profile_ref": "synthetic-coder"},
            "Runtime profile synthetic-coder prices only inference work.",
        ),
        (
            {
                "quantities": [
                    {"metric": "duration", "unit": "minutes", "expected": "120", "uncertainty": "-"}
                ]
            },
            "duration is given in minutes; the price sheet expects seconds.",
        ),
        (
            {"quantities": [{"metric": "duration", "unit": "seconds", "uncertainty": "unknown"}]},
            "The prediction gives no expected value for duration.",
        ),
        ({"quantities": []}, "The prediction gives no duration."),
    ],
)
def test_work_that_the_sheet_cannot_measure_is_unknown(
    changes: dict[str, Any], limitation: str
) -> None:
    quote = _quote(plan=_element(prediction(), "exploration", **changes))

    exploration = _costs(quote)["exploration"]
    assert exploration.coverage == "unknown"
    assert exploration.limitations == (limitation,)
    assert quote.total.coverage == "partial"


def test_batch_without_a_retry_allowance_is_priced_without_retries() -> None:
    plan = prediction()
    (batch,) = [item for item in plan.elements if item.element_id == "batch"]
    quantities = [
        item.model_dump(mode="json")
        for item in batch.quantities
        if item.metric != "retry_allowance"
    ]

    quote = _quote(plan=_element(plan, "batch", quantities=quantities), element_ids=["batch"])

    assert _costs(quote)["batch"].expected == "18"
    assert "Batch work without a retry allowance is priced without retries." in (
        quote.pricing_assumptions
    )


def test_prices_apply_only_in_their_effective_period_and_limit_the_validity() -> None:
    def ending(at: str) -> Callable[[dict[str, Any]], object]:
        def change(document: dict[str, Any]) -> None:
            for price in document["sku_prices"]:
                price["SkuPriceEffectiveEnd"] = at

        return change

    soon = _quote(sheet=_sheet(ending("2026-11-19T12:30:00Z")))
    assert soon.total.coverage == "complete"
    assert soon.valid_until == datetime(2026, 11, 19, 12, 30, tzinfo=UTC)

    ended = _quote(sheet=_sheet(ending("2026-11-19T12:00:00Z")), element_ids=["exploration"])
    assert ended.total.coverage == "unknown"
    assert ended.total.limitations == (
        "SKU price synthetic-exploration-small-list is not in effect.",
    )

    early = _quote(now=datetime(2025, 12, 31, tzinfo=UTC))
    assert early.total.coverage == "unknown"


@pytest.mark.parametrize("element_ids", [[], ["batch", "batch"], ["missing"], ["batch", "missing"]])
def test_element_estimate_needs_distinct_elements_of_the_prediction(
    element_ids: list[str],
) -> None:
    with pytest.raises(EstimateRequestError):
        _quote(element_ids=element_ids)


def test_plan_estimate_names_no_elements() -> None:
    with pytest.raises(EstimateRequestError):
        estimate(_sheet(), prediction(), scope="plan", element_ids=["batch"], now=_NOW)


def _first_price(**changes: Any) -> Callable[[dict[str, Any]], object]:  # noqa: ANN401
    def change(document: dict[str, Any]) -> None:
        document["sku_prices"][0].update(changes)
        document["sku_prices"][0] = {
            key: value for key, value in document["sku_prices"][0].items() if value is not None
        }

    return change


def _first_rate(**changes: Any) -> Callable[[dict[str, Any]], object]:  # noqa: ANN401
    def change(document: dict[str, Any]) -> None:
        document["runtime_profiles"][0]["rates"][0].update(changes)

    return change


def _set(key: str, value: object) -> Callable[[dict[str, Any]], object]:
    return lambda document: document.update({key: value})


def _duplicate(key: str) -> Callable[[dict[str, Any]], object]:
    return lambda document: document[key].append(document[key][0])


def _extra_rate(document: dict[str, Any]) -> None:
    rates = document["runtime_profiles"][0]["rates"]
    rates.append(dict(rates[0]))


def _missing_rate(document: dict[str, Any]) -> None:
    document["runtime_profiles"][3]["rates"].pop()


@pytest.mark.parametrize(
    "change",
    [
        _set("currency", "USD"),
        _first_price(PricingQuantity="1"),
        _first_price(UnitPrice="-0.0005"),
        _first_price(UnitPrice=0.0005),
        _first_price(UnitPrice="5e-4"),
        _first_price(UnitPrice=""),
        _first_price(SkuPriceId=""),
        _first_price(ChargeCategory="Purchase"),
        _first_price(UnitPriceType="Contracted"),
        _first_price(ContractId="contract-1"),
        _first_price(PricingCurrency="usd"),
        _first_price(PricingCurrency="EUR"),
        _first_price(SkuPriceEligibility={"IsGlobalScope": False}),
        _first_price(SkuPriceEligibility={"IsGlobalScope": True, "Exclusions": []}),
        _first_price(SkuPriceEffectiveStart="2026-01-01T00:00:00"),
        _first_price(SkuPriceEffectiveEnd="2025-12-31T00:00:00Z"),
        _duplicate("sku_prices"),
        _duplicate("runtime_profiles"),
        _first_rate(sku_price_id="missing"),
        _first_rate(quantity_per_pricing_unit="0"),
        _first_rate(measure="input_tokens"),
        _extra_rate,
        _missing_rate,
        _set("sku_prices", []),
        _set("estimate_validity_seconds", 0),
    ],
)
def test_price_sheet_is_refused_unless_it_is_consistent(
    change: Callable[[dict[str, Any]], object],
) -> None:
    with pytest.raises(ValidationError):
        _sheet(change)


def test_contract_price_names_its_contract() -> None:
    sheet = _sheet(_first_price(UnitPriceType="Contracted", ContractId="contract-1"))

    assert sheet.sku_prices[0].contract_id == "contract-1"
