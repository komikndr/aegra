import pytest
from aegra_message_gateway.chart_output import (
    MAX_CHART_OPTION_BYTES,
    ChartFragment,
    EChartsFenceExtractor,
    TextFragment,
)


def test_chart_fence_is_withheld_until_the_option_is_complete() -> None:
    extractor = EChartsFenceExtractor()

    first = extractor.feed("Before chart.\n```echarts\n{\"series\":[")
    second = extractor.feed("{\"type\":\"bar\",\"data\":[1]}]}\n```\nAfter chart.")

    assert first == [TextFragment("Before chart.\n")]
    assert second == [
        ChartFragment({"series": [{"type": "bar", "data": [1]}]}),
        TextFragment("\nAfter chart."),
    ]


def test_invalid_or_unclosed_chart_fence_never_returns_raw_option_text() -> None:
    extractor = EChartsFenceExtractor()

    assert extractor.feed("```echarts\n{invalid}\n```") == [ChartFragment(None)]
    assert extractor.feed("```echarts\n{\"series\":[]}", final=True) == [ChartFragment(None)]


@pytest.mark.parametrize("invalid_option", ["{\"value\":NaN}", "{\"value\":Infinity}", "{\"a\":1,\"a\":2}", "[]"])
def test_strict_chart_fence_rejects_non_standard_or_non_object_json(invalid_option: str) -> None:
    extractor = EChartsFenceExtractor()

    fragments = extractor.feed(f"```echarts\n{invalid_option}\n```")

    assert fragments == [ChartFragment(None)]


def test_chart_fence_can_be_split_across_opening_and_closing_boundaries() -> None:
    extractor = EChartsFenceExtractor()

    fragments = [
        *extractor.feed("Before\n```ech"),
        *extractor.feed("arts\n{\"series\":[]}"),
        *extractor.feed("\n``"),
        *extractor.feed("`\nAfter", final=True),
    ]

    assert fragments == [TextFragment("Before\n"), ChartFragment({"series": []}), TextFragment("\nAfter")]


def test_only_lowercase_line_fences_are_withheld_as_charts() -> None:
    extractor = EChartsFenceExtractor()

    fragments = extractor.feed("````echarts\n{\"series\":[]}\n````\n```ECharts\n{\"series\":[]}\n```", final=True)

    assert fragments == [TextFragment("````echarts\n{\"series\":[]}\n````\n```ECharts\n{\"series\":[]}\n```")]


def test_oversized_chart_is_replaced_once_without_leaking_its_json() -> None:
    extractor = EChartsFenceExtractor()

    first = extractor.feed(f"```echarts\n{{\"label\":\"{'x' * MAX_CHART_OPTION_BYTES}\"}}")
    second = extractor.feed("\n```\nAfter", final=True)

    assert first == [ChartFragment(None)]
    assert second == [TextFragment("\nAfter")]
