from zero_dte_pipeline.tech.contracts import EMA_MENTION_RE


def test_ema_mention_regex_edge_cases() -> None:
    # Should match
    should_match = [
        "EMA",
        "ema20",
        "EMA 20",
        "EMA-20",
        "ema(20)",
        "20 EMA",
        "20EMA",
        "20-EMA",
        "20(EMA)",
        "ema_20",
        "20_ema",
    ]

    # Should NOT match (substring hell)
    should_not_match = [
        "demand",
        "team",
        "cinema",
        "rename",
        "tEMAporary",
        "streama",
    ]

    for s in should_match:
        assert EMA_MENTION_RE.search(s), f"expected match: {s}"

    for s in should_not_match:
        assert not EMA_MENTION_RE.search(s), f"expected no match: {s}"
