/* ダッシュボード表示項目のAPI形式と画面形式の変換。DOMや通信へ依存しない。 */
(() => {
  "use strict";

  const apiWidgetTypes = {
    current_state: "現在状態",
    event_count: "イベント回数",
    on_off_duration: "ON/OFF時間",
    last_event: "最終イベント",
    elapsed_time: "経過時間",
    timeline: "タイムライン",
    event_list: "イベント一覧"
  };
  const uiWidgetTypes = Object.fromEntries(
    Object.entries(apiWidgetTypes).map(([apiType, uiType]) => [uiType, apiType])
  );

  // 保存済み期間を画面側の期間選択形式へ戻す。
  function widgetPeriodFromApi(widget) {
    const config = widget.config || {};
    const explicitPeriod = config.ui_period;
    const validPeriods = new Set([
      "follow", "1h", "8h", "24h", "7d", "30d", "today", "yesterday", "this_week", "custom"
    ]);
    if (typeof explicitPeriod === "string" && validPeriods.has(explicitPeriod)) {
      return explicitPeriod;
    }
    const timeRange = config.time_range || { mode: "dashboard" };
    if (timeRange.mode !== "fixed") return "follow";
    if (typeof timeRange.period === "string" && validPeriods.has(timeRange.period)) {
      return timeRange.period;
    }
    const duration = Number(timeRange.duration_ms);
    const periodByDuration = new Map([
      [3_600_000, "1h"],
      [8 * 3_600_000, "8h"],
      [24 * 3_600_000, "24h"],
      [7 * 86_400_000, "7d"],
      [30 * 86_400_000, "30d"]
    ]);
    return periodByDuration.get(duration) || "custom";
  }

  // APIが返すウィジェット定義をフォーム・カード共通の表現へ変換する。
  function widgetConfigFromApi(widget) {
    const rawConfig = widget.config && typeof widget.config === "object" ? widget.config : {};
    const type = apiWidgetTypes[widget.type];
    if (!type) throw new Error(`未対応のウィジェット種別です: ${widget.type}`);
    const rawSignals = Array.isArray(rawConfig.signals)
      ? rawConfig.signals
      : Array.isArray(rawConfig.channels)
        ? rawConfig.channels.map((channel) => `IN${channel}`)
        : [];
    const channels = rawSignals.map((signal) => String(signal).toUpperCase());
    const channelId = channels.length
      ? channels.join(",")
      : `IN${Number(rawConfig.channel_id || 1)}`;
    const countModes = { reset: "since_reset", stored: "stored_total" };
    return {
      id: String(widget.id),
      title: String(widget.title),
      type,
      channelId,
      period: widgetPeriodFromApi(widget),
      edge: rawConfig.edge === "falling" ? "OFF" : "ON",
      state: rawConfig.state === "OFF" ? "OFF" : "ON",
      count: Number(rawConfig.limit || 10),
      value: typeof rawConfig.value === "string" ? rawConfig.value : "all",
      countMode: countModes[String(rawConfig.count_mode)] || "period",
      unit: typeof rawConfig.unit === "string" ? rawConfig.unit : "回",
      timeUnit: typeof rawConfig.display_unit === "string" ? rawConfig.display_unit : "human",
      showAbsolute: rawConfig.show_absolute !== false,
      showEventElapsed: rawConfig.show_elapsed === true,
      onLabel: typeof rawConfig.on_label === "string" ? rawConfig.on_label : "ON",
      offLabel: typeof rawConfig.off_label === "string" ? rawConfig.off_label : "OFF",
      showElapsed: rawConfig.show_elapsed === true,
      source: rawConfig.source === "event" ? "event" : "state",
      durationMs: Number(rawConfig.time_range?.duration_ms || 0),
      apiConfig: rawConfig,
      size: String(widget.size || "small"),
      enabled: widget.enabled !== false
    };
  }

  // 表示の期間指定をAPIが扱う形式へ変換する。
  function widgetTimeRangeFromUi(period, durationMs = 0) {
    if (!period || period === "follow") return { mode: "dashboard" };
    if (["1h", "8h", "24h", "7d", "30d"].includes(period)) {
      return { mode: "fixed", period };
    }
    const durationByPeriod = {
      today: 24 * 3_600_000,
      yesterday: 24 * 3_600_000,
      this_week: 7 * 86_400_000,
      custom: 4 * 3_600_000
    };
    if (period === "custom" && Number.isInteger(durationMs) && durationMs >= 60_000 && durationMs <= 365 * 86_400_000) {
      return { mode: "fixed", duration_ms: durationMs };
    }
    return { mode: "fixed", duration_ms: durationByPeriod[period] || 8 * 3_600_000 };
  }

  // フォームの値を保存用の型とキー名へ変換する。
  function widgetPayloadFromConfig(uiConfig, sortOrder, enabled, widgetSource) {
    const apiType = uiWidgetTypes[uiConfig.type];
    if (!apiType) throw new Error(`未対応のウィジェット種別です: ${uiConfig.type}`);
    const channelIds = String(uiConfig.channelId || "").split(",").filter(Boolean);
    const config = {
      ...(uiConfig.apiConfig || {}),
      time_range: widgetTimeRangeFromUi(uiConfig.period, uiConfig.durationMs),
      ui_period: uiConfig.period
    };
    delete config.channels;
    delete config.channel_id;
    delete config.signals;
    if (["timeline", "event_list"].includes(apiType)) {
      config.signals = channelIds;
    } else {
      config.channels = channelIds.map((channel) => Number(channel.slice(2)));
    }
    if (apiType === "current_state") {
      Object.assign(config, {
        show_elapsed: uiConfig.showElapsed,
        on_label: uiConfig.onLabel,
        off_label: uiConfig.offLabel
      });
    } else if (apiType === "event_count") {
      const countModes = { since_reset: "reset", stored_total: "stored" };
      Object.assign(config, {
        edge: uiConfig.edge === "OFF" ? "falling" : "rising",
        count_mode: countModes[uiConfig.countMode] || "period",
        unit: uiConfig.unit
      });
    } else if (apiType === "on_off_duration") {
      Object.assign(config, { state: uiConfig.state, display_unit: uiConfig.timeUnit });
    } else if (apiType === "last_event") {
      Object.assign(config, {
        edge: uiConfig.edge === "OFF" ? "falling" : "rising",
        show_elapsed: uiConfig.showEventElapsed,
        show_absolute: uiConfig.showAbsolute
      });
    } else if (apiType === "elapsed_time") {
      Object.assign(config, {
        source: widgetSource || "event",
        state: uiConfig.state,
        edge: uiConfig.edge === "OFF" ? "falling" : "rising"
      });
    } else if (apiType === "event_list") {
      Object.assign(config, { limit: uiConfig.count, value: uiConfig.value });
    }
    return {
      id: uiConfig.id,
      type: apiType,
      title: uiConfig.title,
      config,
      size: uiConfig.size,
      sort_order: sortOrder,
      enabled: enabled
    };
  }

  window.EventLoggerWidgetConfig = Object.freeze({
    uiWidgetTypes,
    widgetPeriodFromApi,
    widgetConfigFromApi,
    widgetTimeRangeFromUi,
    widgetPayloadFromConfig
  });
})();
