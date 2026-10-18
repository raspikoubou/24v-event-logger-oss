/* ダッシュボード・ログ検索のAPIクエリ生成。DOMとHTTP通信から切り離す。 */
(() => {
  "use strict";

  // ダッシュボードの期間指定をAPIクエリに変換する。
  function dashboardApiUrl(period, customRange) {
    const query = new URLSearchParams();
    query.set("period", period || "8h");
    if (
      period === "custom"
      && Number.isInteger(customRange?.from_ms)
      && Number.isInteger(customRange?.to_ms)
    ) {
      query.set("from_ms", String(customRange.from_ms));
      query.set("to_ms", String(customRange.to_ms));
    }
    return `/api/dashboard?${query.toString()}`;
  }

  // フィルター・時間範囲・ページ条件を一覧表示とCSVで共有する。
  function buildLogApiQuery({
    periodKey, customBounds, logSearchRange, selectedSignal, widgetLogSignals,
    selectedValue, selectedKind, selectedSource, includePaging, pageSize, logPage
  }) {
    const query = new URLSearchParams();
    if (periodKey === "custom") {
      query.set("from_ms", String(Math.floor(customBounds.startMs)));
      query.set("to_ms", String(Math.floor(customBounds.endMs)));
    } else if (logSearchRange) {
      query.set("from_ms", String(logSearchRange.fromMs));
      query.set("to_ms", String(logSearchRange.toMs));
    } else {
      query.set("period", periodKey);
    }
    if (selectedSignal === "selection" && widgetLogSignals?.length) {
      query.set("signal", widgetLogSignals.join(","));
    } else if (selectedSignal !== "all" && selectedSignal !== "selection") {
      query.set("signal", selectedSignal);
    }
    if (selectedValue !== "all") query.set("value", selectedValue);
    if (selectedKind !== "all") query.set("kind", selectedKind);
    if (selectedSource !== "all") query.set("source", selectedSource);
    if (includePaging) {
      query.set("include_total_count", "1");
      query.set("limit", String(pageSize));
      query.set("offset", String((logPage - 1) * pageSize));
    }
    return query.toString();
  }

  // 選択ウィジェットの種類からログ画面へ引き継ぐ値フィルターを決める。
  function logValueFilterForWidget(widgetType, widgetValue) {
    if (widgetType !== "イベント一覧") return "all";
    return ["ON", "OFF"].includes(widgetValue) ? widgetValue : "all";
  }

  window.EventLoggerQueryPolicy = Object.freeze({
    dashboardApiUrl, buildLogApiQuery, logValueFilterForWidget
  });
})();
