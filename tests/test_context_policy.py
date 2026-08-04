from dataclasses import FrozenInstanceError

import pytest

from cli_agent.runtime import ContextPolicy


def test_computes_input_budget_only_from_explicit_configuration() -> None:
    policy = ContextPolicy(
        context_window_tokens=128_000,
        output_reserve_tokens=4_000,
        safety_margin_tokens=2_000,
    )

    assert policy.input_budget == 122_000
    assert policy.snip_threshold == 0.60
    assert policy.prune_threshold == 0.80
    assert policy.summarize_threshold == 0.95
    assert policy.snip_target == 0.55
    assert policy.prune_target == 0.70
    assert policy.summarize_target == 0.55
    assert policy.protected_tokens == 8_000
    assert policy.minimum_reclaim_tokens == 4_096
    assert policy.excluded_tools == frozenset()


def test_accepts_minimal_positive_input_budget() -> None:
    policy = ContextPolicy(
        context_window_tokens=10,
        output_reserve_tokens=6,
        safety_margin_tokens=3,
    )

    assert policy.input_budget == 1


def test_policy_is_immutable() -> None:
    policy = ContextPolicy(
        context_window_tokens=128_000,
        output_reserve_tokens=4_000,
        safety_margin_tokens=0,
    )

    with pytest.raises(FrozenInstanceError):
        policy.protected_tokens = 1_000  # type: ignore[misc]


@pytest.mark.parametrize(
    ("policy", "error_match"),
    (
        (
            {
                "context_window_tokens": 128_000,
                "output_reserve_tokens": 0,
                "safety_margin_tokens": 0,
            },
            "output_reserve_tokens must be a positive integer",
        ),
        (
            {
                "context_window_tokens": 128_000,
                "output_reserve_tokens": 4_000,
                "safety_margin_tokens": -1,
            },
            "safety_margin_tokens must be a non-negative integer",
        ),
        (
            {
                "context_window_tokens": 128_000,
                "output_reserve_tokens": 128_000,
                "safety_margin_tokens": 0,
            },
            "input budget is positive",
        ),
        (
            {
                "context_window_tokens": 128_000,
                "output_reserve_tokens": 120_000,
                "safety_margin_tokens": 10_000,
            },
            "input budget is positive",
        ),
        (
            {
                "context_window_tokens": 128_000,
                "output_reserve_tokens": 4_000,
                "safety_margin_tokens": 0,
                "snip_threshold": 0.0,
            },
            "thresholds must satisfy",
        ),
        (
            {
                "context_window_tokens": 128_000,
                "output_reserve_tokens": 4_000,
                "safety_margin_tokens": 0,
                "snip_threshold": 0.80,
                "prune_threshold": 0.80,
            },
            "thresholds must satisfy",
        ),
        (
            {
                "context_window_tokens": 128_000,
                "output_reserve_tokens": 4_000,
                "safety_margin_tokens": 0,
                "prune_threshold": 0.95,
                "summarize_threshold": 0.90,
            },
            "thresholds must satisfy",
        ),
        (
            {
                "context_window_tokens": 128_000,
                "output_reserve_tokens": 4_000,
                "safety_margin_tokens": 0,
                "summarize_threshold": 1.0,
            },
            "thresholds must satisfy",
        ),
        (
            {
                "context_window_tokens": 128_000,
                "output_reserve_tokens": 4_000,
                "safety_margin_tokens": 0,
                "snip_target": 0.0,
            },
            "snip_target must be between 0 and snip_threshold",
        ),
        (
            {
                "context_window_tokens": 128_000,
                "output_reserve_tokens": 4_000,
                "safety_margin_tokens": 0,
                "snip_target": 0.60,
            },
            "snip_target must be between 0 and snip_threshold",
        ),
        (
            {
                "context_window_tokens": 128_000,
                "output_reserve_tokens": 4_000,
                "safety_margin_tokens": 0,
                "prune_target": 0.80,
            },
            "prune_target must be between 0 and prune_threshold",
        ),
        (
            {
                "context_window_tokens": 128_000,
                "output_reserve_tokens": 4_000,
                "safety_margin_tokens": 0,
                "summarize_target": 0.95,
            },
            "summarize_target must be between 0 and summarize_threshold",
        ),
        (
            {
                "context_window_tokens": 128_000,
                "output_reserve_tokens": 4_000,
                "safety_margin_tokens": 0,
                "protected_tokens": -1,
            },
            "protected_tokens must be a non-negative integer",
        ),
        (
            {
                "context_window_tokens": 128_000,
                "output_reserve_tokens": 4_000,
                "safety_margin_tokens": 0,
                "minimum_reclaim_tokens": -1,
            },
            "minimum_reclaim_tokens must be a non-negative integer",
        ),
    ),
)
def test_rejects_invalid_budget_and_threshold_combinations(
    policy: dict[str, object],
    error_match: str,
) -> None:
    with pytest.raises(ValueError, match=error_match):
        ContextPolicy(**policy)


def test_accepts_customized_thresholds_within_bounds() -> None:
    policy = ContextPolicy(
        context_window_tokens=32_768,
        output_reserve_tokens=2_048,
        safety_margin_tokens=0,
        snip_threshold=0.50,
        prune_threshold=0.70,
        summarize_threshold=0.90,
        snip_target=0.45,
        prune_target=0.60,
        summarize_target=0.50,
        excluded_tools=frozenset({"exec"}),
    )

    assert policy.input_budget == 30_720
    assert policy.excluded_tools == frozenset({"exec"})
