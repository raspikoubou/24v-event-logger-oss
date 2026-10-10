"""UTC保存時刻をユーザー設定timezoneへ変換する。"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo


def format_timestamp(timestamp_ms: int, timezone_name: str) -> str:
    """Unix epochミリ秒を設定timezoneのISO風文字列へ変換する。"""
    timezone = ZoneInfo(timezone_name)
    timestamp = datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC).astimezone(timezone)
    offset = timestamp.utcoffset()
    if offset is None:
        offset_text = ""
    else:
        # strftimeは1行ずつ呼ぶと100万件CSVで大きなCPU負荷になるため、
        # ZoneInfoが返すoffsetを%zと同じ形へ整数演算で組み立てる。
        offset_microseconds = (
            offset.days * 86_400 + offset.seconds
        ) * 1_000_000 + offset.microseconds
        sign = "+" if offset_microseconds >= 0 else "-"
        hours, remainder = divmod(abs(offset_microseconds), 3_600_000_000)
        minutes, remainder = divmod(remainder, 60_000_000)
        seconds, microseconds = divmod(remainder, 1_000_000)
        offset_text = f"{sign}{hours:02d}{minutes:02d}"
        if seconds or microseconds:
            offset_text += f"{seconds:02d}"
        if microseconds:
            offset_text += f".{microseconds:06d}"

    return (
        f"{timestamp.year:04d}-{timestamp.month:02d}-{timestamp.day:02d} "
        f"{timestamp.hour:02d}:{timestamp.minute:02d}:{timestamp.second:02d}."
        f"{timestamp.microsecond // 1000:03d}{offset_text}"
    )
