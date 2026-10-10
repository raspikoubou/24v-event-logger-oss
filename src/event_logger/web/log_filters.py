"""ログ画面のHTTPクエリ検証とローカル日時の相互変換。

Flask requestの解釈をルート登録・DB検索から切り離し、
従来のRepositoryフィルタ形式とエラー契約を維持する。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from flask import request

from event_logger.adapters.clock import Clock
from event_logger.domain.widgets import WIDGET_PERIOD_DURATIONS_MS
from event_logger.web.dashboard_periods import _dashboard_range_ms


def _parse_log_filters(clock: Clock, timezone_name: str) -> dict[str, Any]:
    """HTTP queryをRepository filterへ変換する。"""
    now_ms = clock.now_utc_ms()
    period = request.args.get("period")
    if period is not None:
        if any(name in request.args for name in ("from", "to", "from_ms", "to_ms")):
            raise ValueError("period cannot be combined with a custom range")
        supported_periods = {
            *WIDGET_PERIOD_DURATIONS_MS,
            "today",
            "yesterday",
            "this_week",
        }
        if period not in supported_periods:
            raise ValueError("period is not supported")
        start_ms, end_ms = _dashboard_range_ms(
            clock,
            timezone_name=timezone_name,
            period=period,
        )
    else:
        start_text = request.args.get("from")
        end_text = request.args.get("to")
        start_ms = (
            _parse_local_input(start_text, timezone_name)
            if start_text
            else _query_int("from_ms", now_ms - 24 * 60 * 60 * 1000)
        )
        end_ms = (
            _parse_local_input(end_text, timezone_name)
            if end_text
            else _query_int("to_ms", now_ms + 1)
        )
    if start_ms >= end_ms:
        raise ValueError("from_ms must be before to_ms")
    channel_value = request.args.get("channel_id")
    signals: list[str] | None = None
    if channel_value:
        channel_id = _query_int("channel_id", 0)
        if not 1 <= channel_id <= 6:
            raise ValueError("channel_id must be between 1 and 6")
        signals = [f"IN{channel_id}"]
    signal_value = request.args.get("signal")
    if signal_value:
        signals = [
            part.strip().upper() for part in signal_value.split(",") if part.strip()
        ]
        if not signals:
            raise ValueError("signal must not be empty")
    values = _parse_log_values(request.args.get("value"))
    kinds = _parse_log_choices(
        request.args.get("kind"),
        allowed={"edge", "snapshot", "output"},
        field_name="kind",
    )
    sources = _parse_log_choices(
        request.args.get("source"),
        allowed={"rule", "heartbeat", "test", "startup"},
        field_name="source",
    )
    limit = _query_int("limit", 100)
    offset = _query_int("offset", 0)
    if limit not in (50, 100, 500):
        raise ValueError("limit must be 50, 100, or 500")
    if offset < 0:
        raise ValueError("offset must not be negative")
    return {
        "start_ms": start_ms,
        "end_ms": end_ms,
        "signals": signals,
        "values": values,
        "kinds": kinds,
        "sources": sources,
        "limit": limit,
        "offset": offset,
    }


def _parse_local_input(value: str, timezone_name: str) -> int:
    """設定timezoneのdatetime-local文字列をUTCミリ秒へ変換する。"""
    try:
        parsed = datetime.fromisoformat(value)
        timezone = ZoneInfo(timezone_name)
    except (TypeError, ValueError, KeyError) as error:
        raise ValueError("date/time must be a valid local datetime") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone)
    else:
        parsed = parsed.astimezone(timezone)
    return round(parsed.timestamp() * 1000)


def _format_local_input(timestamp_ms: int, timezone_name: str) -> str:
    """UTCミリ秒を設定timezoneのdatetime-local文字列へ変換する。"""
    try:
        timezone = ZoneInfo(timezone_name)
    except KeyError:
        timezone = ZoneInfo("UTC")
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )


def _parse_log_values(value: str | None) -> list[int] | None:
    """ON/OFF filterを0/1のbind値へ変換する。"""
    if not value:
        return None
    values: list[int] = []
    for item in value.split(","):
        normalized = item.strip().lower()
        if normalized in {"on", "1"}:
            parsed = 1
        elif normalized in {"off", "0"}:
            parsed = 0
        else:
            raise ValueError("value must contain on or off")
        if parsed not in values:
            values.append(parsed)
    return values


def _parse_log_choices(
    value: str | None,
    *,
    allowed: set[str],
    field_name: str,
) -> list[str] | None:
    """カンマ区切りの固定選択filterを検証する。"""
    if not value:
        return None
    choices: list[str] = []
    for item in value.split(","):
        normalized = item.strip().lower()
        if normalized not in allowed:
            raise ValueError(f"{field_name} contains an unsupported value")
        if normalized not in choices:
            choices.append(normalized)
    return choices


def _query_int(name: str, default: int) -> int:
    """整数query parameterを検証する。"""
    value = request.args.get(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error


