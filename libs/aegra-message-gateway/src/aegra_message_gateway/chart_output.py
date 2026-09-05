"""Incremental extraction of strict ECharts Markdown blocks."""

import json
import re
from dataclasses import dataclass
from typing import Any

_ECHARTS_OPEN = re.compile(r"(?<![^\n])```echarts[ \t]*\r?\n")
_ECHARTS_CLOSE = re.compile(r"(?<![^\n])```[ \t]*(?=\r?\n|\Z)")
_ECHARTS_OPEN_PREFIX = "```echarts"
MAX_CHART_OPTION_BYTES = 256 * 1024


@dataclass(frozen=True)
class TextFragment:
    text: str


@dataclass(frozen=True)
class ChartFragment:
    option: dict[str, Any] | None


OutputFragment = TextFragment | ChartFragment


class EChartsFenceExtractor:
    """Withhold fenced chart JSON until it can be rendered instead of sent as text."""

    def __init__(self) -> None:
        self._buffer = ""
        self._chart_option = ""
        self._collecting_chart = False
        self._discarding_chart = False

    def feed(self, delta: str, *, final: bool = False) -> list[OutputFragment]:
        self._buffer += delta
        fragments: list[OutputFragment] = []
        while self._buffer:
            if self._collecting_chart:
                closing = _ECHARTS_CLOSE.search(self._buffer)
                if closing is None:
                    visible, pending = self._split_pending_closing_fence(self._buffer)
                    self._buffer = pending
                    if self._discarding_chart:
                        if final:
                            self._collecting_chart = False
                            self._discarding_chart = False
                            self._buffer = ""
                        break
                    self._chart_option += visible
                    if len(self._chart_option.encode()) > MAX_CHART_OPTION_BYTES:
                        self._chart_option = ""
                        self._discarding_chart = True
                        fragments.append(ChartFragment(None))
                    elif final:
                        self._chart_option = ""
                        self._collecting_chart = False
                        self._buffer = ""
                        fragments.append(ChartFragment(None))
                    break

                if self._discarding_chart:
                    self._collecting_chart = False
                    self._discarding_chart = False
                    self._buffer = self._buffer[closing.end() :]
                    continue
                raw_option = self._chart_option + self._buffer[: closing.start()]
                self._chart_option = ""
                self._collecting_chart = False
                self._discarding_chart = False
                self._buffer = self._buffer[closing.end() :]
                fragments.append(ChartFragment(self._parse_option(raw_option)))
                continue

            opening = _ECHARTS_OPEN.search(self._buffer)
            if opening is None:
                if final:
                    fragments.append(TextFragment(self._buffer))
                    self._buffer = ""
                else:
                    visible, pending = self._split_pending_opening_fence(self._buffer)
                    if visible:
                        fragments.append(TextFragment(visible))
                    self._buffer = pending
                break

            if opening.start() > 0:
                fragments.append(TextFragment(self._buffer[: opening.start()]))
            self._buffer = self._buffer[opening.end() :]
            self._collecting_chart = True
        if final and self._collecting_chart:
            self._buffer = ""
            self._chart_option = ""
            self._collecting_chart = False
            self._discarding_chart = False
            fragments.append(ChartFragment(None))
        return [fragment for fragment in fragments if not isinstance(fragment, TextFragment) or fragment.text]

    @staticmethod
    def _parse_option(raw_option: str) -> dict[str, Any] | None:
        try:
            parsed = json.loads(
                raw_option.strip(),
                parse_constant=EChartsFenceExtractor._reject_nonstandard_constant,
                object_pairs_hook=EChartsFenceExtractor._reject_duplicate_keys,
            )
        except (json.JSONDecodeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None

    @staticmethod
    def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate key: {key}")
            result[key] = value
        return result

    @staticmethod
    def _reject_nonstandard_constant(value: str) -> None:
        raise ValueError(f"invalid JSON constant: {value}")

    @staticmethod
    def _split_pending_opening_fence(value: str) -> tuple[str, str]:
        line_start = value.rfind("\n") + 1
        candidate = value[line_start:]
        if _ECHARTS_OPEN_PREFIX.startswith(candidate):
            return value[:line_start], candidate
        if candidate.startswith(_ECHARTS_OPEN_PREFIX) and re.fullmatch(r"```echarts[ \t]*\r?", candidate):
            return value[:line_start], candidate
        return value, ""

    @staticmethod
    def _split_pending_closing_fence(value: str) -> tuple[str, str]:
        line_start = value.rfind("\n") + 1
        candidate = value[line_start:]
        if candidate == "":
            return value[:line_start], value[line_start - 1 :]
        if re.fullmatch(r"`{1,3}[ \t]*\r?", candidate):
            return value[:line_start], candidate
        return value, ""
