"""ウィジェット設定の固定スキーマと入力検証。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

# Dashboardの共通期間とwidget固有期間で共有する固定選択肢。
# 自由な式やSQLを設定値として受け取らず、保存データ量を予測できる範囲に限定する。
WIDGET_PERIOD_DURATIONS_MS: dict[str, int] = {
    "1h": 60 * 60 * 1000,
    "8h": 8 * 60 * 60 * 1000,
    "24h": 24 * 60 * 60 * 1000,
    "7d": 7 * 24 * 60 * 60 * 1000,
    "30d": 30 * 24 * 60 * 60 * 1000,
}


class WidgetType(StrEnum):
    """初期版で提供するウィジェット種別。"""

    CURRENT_STATE = "current_state"
    EVENT_COUNT = "event_count"
    ON_OFF_DURATION = "on_off_duration"
    LAST_EVENT = "last_event"
    ELAPSED_TIME = "elapsed_time"
    TIMELINE = "timeline"
    EVENT_LIST = "event_list"


class WidgetSize(StrEnum):
    """レスポンシブグリッドで選択できる3段階サイズ。"""

    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"


@dataclass(frozen=True, slots=True)
class Widget:
    """Dashboardに表示する永続ウィジェット設定。"""

    id: str
    widget_type: WidgetType
    title: str
    config: Mapping[str, object]
    size: WidgetSize
    sort_order: int
    enabled: bool
    updated_at_ms: int

    def __post_init__(self) -> None:
        """DB/API境界で設定値を検証する。

        Args:
            なし。

        Returns:
            None。 制約を検証するだけで、 永続状態や入力値は変更しない。

        Raises:
            ValueError: 「widget title must be 1..200
                characters」に該当すると判定した場合。 widget sort_orderが負の場合。
        """
        validate_widget_id(self.id)
        if not self.title.strip() or len(self.title) > 200:
            raise ValueError("widget title must be 1..200 characters")
        if self.sort_order < 0:
            raise ValueError("widget sort_order must not be negative")
        validate_widget_config(self.widget_type, self.config)


def validate_widget_id(widget_id: str) -> None:
    """widget IDの長さだけを固定し、将来の複製方式を妨げない。

    Args:
        widget_id: 対象widgetの永続ID。 関連データと監査記録のkeyに使う。

    Returns:
        None。 制約を検証するだけで、 永続状態や入力値は変更しない。

    Raises:
        ValueError: 「widget id must be 1..100 characters」に該当すると判定した場合。
    """
    if not widget_id or len(widget_id) > 100:
        raise ValueError("widget id must be 1..100 characters")


def normalize_widget_config(payload: Mapping[str, object]) -> dict[str, object]:
    """API payloadの信号表記を固定JSONへ正規化する。

    既存の入力専用widgetは整数の ``channels`` をそのまま保持する。
    タイムラインとイベント一覧だけは、 OUTを含む複数信号を表せるよう
    ``signals``（``IN1`` / ``OUT1``）へ分離して保存する。

    Args:
        payload: APIから受け取ったJSON相当mapping。 正規化時は入力mappingを変更しない。

    Returns:
        widget APIへ返す表示field/value mapping。
    """
    config = dict(payload)
    if "channels" not in config and "channel_id" in config:
        config["channels"] = [config.pop("channel_id")]
    channels = config.get("channels")
    if (
        "signals" not in config
        and isinstance(channels, list)
        and any(isinstance(channel, str) for channel in channels)
    ):
        config["signals"] = list(channels)
        config.pop("channels", None)
    channels = config.get("channels")
    if "channel_id" not in config and isinstance(channels, list) and len(channels) == 1:
        # retentionのcounter carryは単一入力条件をRepositoryで照合するため、
        # Dashboardの共通channels表記に加えて互換キーも固定保存する。
        config["channel_id"] = channels[0]
    if "time_range" not in config:
        config["time_range"] = {"mode": "dashboard"}
    return config


def validate_widget_config(
    widget_type: WidgetType, config: Mapping[str, object]
) -> None:
    """種別ごとの固定設定を検証する。

    計算式やSQLを受け取る自由形式設定は許可しない。 未使用の拡張キーは
    将来互換性のため保持できるが、 主要フィールドの型と範囲は固定する。

    Args:
        widget_type: configのschemaと評価方法を決めるWidgetType。
        config: widgetまたはruleのJSON相当設定mapping。 式やSQLとして実行しない。

    Returns:
        None。 制約を検証するだけで、 永続状態や入力値は変更しない。

    Raises:
        ValueError: 「widget config must be an object」に該当すると判定した場合。
            「free-form expressions are not supported」に該当すると判定した場合。
            「widget channel_id must be an integer」に該当すると判定した場合。 「widget
            channel_id must match channels[0]」に該当すると判定した場合。 「event_count
            count_mode is invalid」に該当すると判定した場合。 「on_off_duration state
            must be ON or OFF」に該当すると判定した場合。 「elapsed_time source must be
            state or event」に該当すると判定した場合。 「elapsed_time state must be ON
            or OFF」に該当すると判定した場合。 event_list limitが1〜100の範囲外の場合。
    """
    if not isinstance(config, Mapping):
        raise ValueError("widget config must be an object")
    forbidden_keys = {
        "sql",
        "query",
        "expression",
        "formula",
        "command",
        "shell",
        "python",
    }
    if any(str(key).lower() in forbidden_keys for key in config):
        raise ValueError("free-form expressions are not supported")
    _time_range(config)
    signal_list = _signals(config) if "signals" in config else None
    channels = _channels(config) if signal_list is None else []
    raw_channel_id = config.get("channel_id")
    if raw_channel_id is not None:
        if isinstance(raw_channel_id, bool) or not isinstance(raw_channel_id, int):
            raise ValueError("widget channel_id must be an integer")
        if not channels or raw_channel_id != channels[0]:
            raise ValueError("widget channel_id must match channels[0]")
    if widget_type in {WidgetType.TIMELINE, WidgetType.EVENT_LIST}:
        if signal_list is not None:
            if not signal_list:
                raise ValueError(f"{widget_type.value} requires at least one signal")
        elif not channels:
            raise ValueError(f"{widget_type.value} requires at least one channel")
    elif len(channels) != 1:
        raise ValueError(f"{widget_type.value} requires exactly one channel")

    if widget_type is WidgetType.CURRENT_STATE:
        _optional_bool(config, "show_elapsed")
        _optional_text(config, "on_label")
        _optional_text(config, "off_label")
    elif widget_type is WidgetType.EVENT_COUNT:
        _edge(config)
        mode = config.get("count_mode", "period")
        if mode not in {"period", "reset", "stored"}:
            raise ValueError("event_count count_mode is invalid")
        _optional_text(config, "unit")
    elif widget_type is WidgetType.ON_OFF_DURATION:
        state = config.get("state", "ON")
        if state not in {"ON", "OFF"}:
            raise ValueError("on_off_duration state must be ON or OFF")
        _optional_text(config, "display_unit")
    elif widget_type is WidgetType.LAST_EVENT:
        _edge(config)
        _optional_bool(config, "show_elapsed")
    elif widget_type is WidgetType.ELAPSED_TIME:
        source = config.get("source", "state")
        if source not in {"state", "event"}:
            raise ValueError("elapsed_time source must be state or event")
        if source == "state":
            state = config.get("state", "ON")
            if state not in {"ON", "OFF"}:
                raise ValueError("elapsed_time state must be ON or OFF")
        else:
            _edge(config)
    elif widget_type is WidgetType.EVENT_LIST:
        limit = config.get("limit", 10)
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 100
        ):
            raise ValueError("event_list limit must be between 1 and 100")
    elif widget_type is not WidgetType.TIMELINE:
        raise ValueError(f"unsupported widget type: {widget_type.value}")


def _channels(config: Mapping[str, object]) -> list[int]:
    """configから重複のないIN/OUT channel番号を取得する。

    Args:
        config: widgetまたはruleのJSON相当設定mapping。 式やSQLとして実行しない。

    Returns:
        条件に一致したrowまたは表示値列。 該当なしは空list。

    Raises:
        ValueError: 「widget channels must be a non-empty
            list」に該当すると判定した場合。 「widget channel must be an
            integer」に該当すると判定した場合。 widget channelが1〜6の範囲外の場合。
            「widget channels must not contain duplicates」に該当すると判定した場合。
    """
    raw_channels = config.get("channels")
    if not isinstance(raw_channels, list) or not raw_channels:
        raise ValueError("widget channels must be a non-empty list")
    channels: list[int] = []
    for raw_channel in raw_channels:
        if isinstance(raw_channel, bool) or not isinstance(raw_channel, int):
            raise ValueError("widget channel must be an integer")
        if not 1 <= raw_channel <= 6:
            raise ValueError("widget channel must be between 1 and 6")
        if raw_channel in channels:
            raise ValueError("widget channels must not contain duplicates")
        channels.append(raw_channel)
    return channels


def _signals(config: Mapping[str, object]) -> list[str]:
    """configから重複のないIN/OUT信号名を取得する。

    Args:
        config: widgetまたはruleのJSON相当設定mapping。 式やSQLとして実行しない。

    Returns:
        指定signalを入力順に正規化したlist。

    Raises:
        ValueError: 「widget signals must be a non-empty
            list」に該当すると判定した場合。 「widget signal must be a
            string」に該当すると判定した場合。 「widget signals must contain IN1..IN6 or
            OUT1..OUT6」に該当すると判定した場合。 widget signal
            channelが1〜6の範囲外の場合。 「widget signals must not contain
            duplicates」に該当すると判定した場合。 「widget signal channel must be
            numeric」に該当すると判定した場合。
    """
    raw_signals = config.get("signals")
    if not isinstance(raw_signals, list) or not raw_signals:
        raise ValueError("widget signals must be a non-empty list")
    signals: list[str] = []
    for raw_signal in raw_signals:
        if not isinstance(raw_signal, str):
            raise ValueError("widget signal must be a string")
        signal = raw_signal.upper()
        if (
            (not signal.startswith("IN") and not signal.startswith("OUT"))
            or (signal.startswith("IN") and len(signal) != 3)
            or (signal.startswith("OUT") and len(signal) != 4)
        ):
            raise ValueError("widget signals must contain IN1..IN6 or OUT1..OUT6")
        prefix = "IN" if signal.startswith("IN") else "OUT"
        try:
            channel_id = int(signal[len(prefix) :])
        except ValueError as error:
            raise ValueError("widget signal channel must be numeric") from error
        if not 1 <= channel_id <= 6:
            raise ValueError("widget signal channel must be between 1 and 6")
        canonical = f"{prefix}{channel_id}"
        if canonical in signals:
            raise ValueError("widget signals must not contain duplicates")
        signals.append(canonical)
    return signals


def _edge(config: Mapping[str, object]) -> str:
    """event系configのedgeを検証する。

    Args:
        config: widgetまたはruleのJSON相当設定mapping。 式やSQLとして実行しない。

    Returns:
        検査または正規化を通過した文字列値。

    Raises:
        ValueError: 「widget edge must be rising or falling」に該当すると判定した場合。
    """
    edge = config.get("edge", "rising")
    if edge not in {"rising", "falling"}:
        raise ValueError("widget edge must be rising or falling")
    return str(edge)


def _optional_bool(config: Mapping[str, object], key: str) -> None:
    """任意boolean設定を検証する。

    Args:
        config: widgetまたはruleのJSON相当設定mapping。 式やSQLとして実行しない。
        key: 設定項目名。 保存値の取得または更新対象を特定する。

    Returns:
        None。 指定config keyが省略またはbool型であることだけを検証し、
            configは変更しない。

    Raises:
        ValueError: deviceまたはSQLite操作で失敗が返された場合。
    """
    value = config.get(key)
    if value is not None and not isinstance(value, bool):
        raise ValueError(f"widget {key} must be a boolean")


def _optional_text(config: Mapping[str, object], key: str) -> None:
    """任意文字列設定を検証する。

    Args:
        config: widgetまたはruleのJSON相当設定mapping。 式やSQLとして実行しない。
        key: 設定項目名。 保存値の取得または更新対象を特定する。

    Returns:
        None。 指定config keyが省略または200文字以下の文字列であることを検証する。

    Raises:
        ValueError: deviceまたはSQLite操作で失敗が返された場合。
    """
    value = config.get(key)
    if value is not None and (not isinstance(value, str) or len(value) > 200):
        raise ValueError(f"widget {key} must be a string up to 200 characters")


def _time_range(config: Mapping[str, object]) -> tuple[str, int | None]:
    """widget期間の固定スキーマを検証して、modeと長さを返す。

    Args:
        config: widgetまたはruleのJSON相当設定mapping。 式やSQLとして実行しない。

    Returns:
        注釈に示す順序の集計値tuple。

    Raises:
        ValueError: 「widget time_range must be an object」に該当すると判定した場合。
            「widget time_range mode must be dashboard or
            fixed」に該当すると判定した場合。 widget fixed duration_msが1 minute〜365
            daysの範囲外の場合。 「dashboard time_range must not define a fixed
            period」に該当すると判定した場合。 「widget fixed period is
            invalid」に該当すると判定した場合。 「widget fixed time_range must use
            period or duration_ms」に該当すると判定した場合。
    """
    raw = config.get("time_range", {"mode": "dashboard"})
    if not isinstance(raw, Mapping):
        raise ValueError("widget time_range must be an object")
    mode = raw.get("mode", "dashboard")
    if mode == "dashboard":
        if "period" in raw or "duration_ms" in raw:
            raise ValueError("dashboard time_range must not define a fixed period")
        return "dashboard", None
    if mode != "fixed":
        raise ValueError("widget time_range mode must be dashboard or fixed")
    period = raw.get("period")
    duration_ms = raw.get("duration_ms")
    if period is not None:
        if not isinstance(period, str) or period not in WIDGET_PERIOD_DURATIONS_MS:
            raise ValueError("widget fixed period is invalid")
        if duration_ms is not None:
            raise ValueError("widget fixed time_range must use period or duration_ms")
        return "fixed", WIDGET_PERIOD_DURATIONS_MS[period]
    if (
        isinstance(duration_ms, bool)
        or not isinstance(duration_ms, int)
        or not 60 * 1000 <= duration_ms <= 365 * 24 * 60 * 60 * 1000
    ):
        raise ValueError(
            "widget fixed duration_ms must be between 1 minute and 365 days"
        )
    return "fixed", duration_ms


def widget_start_ms(widget: Widget, *, dashboard_start_ms: int, now_ms: int) -> int:
    """widgetの実効開始時刻を返す。

    Dashboardから渡された期間を標準とし、 固定期間widgetだけを現在時計から
    再計算する。 開始時刻は現在時刻より前へ丸め、 空の期間をDomainへ渡さない。

    Args:
        widget: 表示・集計対象のWidget設定。 信号と期間はconfigで決まる。
        dashboard_start_ms: dashboard共通期間の開始時刻（UTC epoch ms）。
        now_ms: 現在時刻（UTC epoch ms）。 期間やwall-clock表示の基準。

    Returns:
        経過時間または継続時間（ms）。 UNKNOWNは確定時間に含めない。
    """
    mode, duration_ms = _time_range(widget.config)
    if mode == "dashboard" or duration_ms is None:
        return dashboard_start_ms
    return max(0, min(now_ms - 1, now_ms - duration_ms))


def widget_end_ms(widget: Widget, *, dashboard_end_ms: int, now_ms: int) -> int:
    """widgetの実効終了時刻を返す。

    固定期間widgetはDashboard共通期間の終了点に追従せず、 現在時刻までを
    表示する。 追従widgetは選択された期間の終了点を使い、 昨日など過去期間へ
    今日のeventを混ぜない。

    Args:
        widget: 表示・集計対象のWidget設定。 信号と期間はconfigで決まる。
        dashboard_end_ms: dashboard共通期間の排他的終了時刻（UTC epoch ms）。
        now_ms: 現在時刻（UTC epoch ms）。 期間やwall-clock表示の基準。

    Returns:
        経過時間または継続時間（ms）。 UNKNOWNは確定時間に含めない。
    """
    mode, _ = _time_range(widget.config)
    if mode == "fixed":
        return now_ms
    return min(now_ms, dashboard_end_ms)


__all__ = [
    "Widget",
    "WidgetSize",
    "WidgetType",
    "WIDGET_PERIOD_DURATIONS_MS",
    "normalize_widget_config",
    "validate_widget_config",
    "widget_end_ms",
    "widget_start_ms",
]
