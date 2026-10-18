/* ダッシュボード・ログ検索のAPIクエリ生成。DOMとHTTP通信から切り離す。 */
(() => {
  "use strict";

  /**
   * Dashboard期間条件をAPI URLへ符号化する。
   * @param {string} period - 期間選択値。
   * @param {Object|null} customRange - 任意期間の境界値。
   * @returns {string} /api/dashboard URL。period省略時は8hを指定し、period=customかつfrom_ms/to_msが整数なら両境界をqueryへ加える。
   */
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

  /**
   * ログ検索条件をAPI query parameterへ変換する。
   * @param {Object} options - 期間範囲、signal/value/kind/source filters、paging設定をまとめたquery組立入力。
   * @param {string} options.periodKey - 期間key。
   * @param {Object} options.customBounds - 利用者が選んだ開始・終了境界。
   * @param {Object|null} options.logSearchRange - ログ検索期間条件。
   * @param {string} options.selectedSignal - 選択中信号のfilter。
   * @param {Array<string>} options.widgetLogSignals - widget表示のため要求するsignal集合。
   * @param {string} options.selectedValue - ON/OFF filter。
   * @param {string} options.selectedKind - event kind filter。
   * @param {string} options.selectedSource - 記録source filter。
   * @param {boolean} options.includePaging - paging queryを含めるか。
   * @param {number} options.pageSize - page内の最大記録件数。
   * @param {number} options.logPage - 取得するpage番号。
   * @returns {string} ?を含まないquery文字列。period/custom range、signals、value/kind/source、paging offsetを反映する。
   */
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

  /**
   * widget値設定をログAPIのON/OFF filterへ対応付ける。
   * @param {string} widgetType - widget種別。
   * @param {string} widgetValue - widget設定値。
   * @returns {string} ログvalue filter。イベント一覧widgetでON/OFF指定が有効ならその値、それ以外はall。
   */
  function logValueFilterForWidget(widgetType, widgetValue) {
    if (widgetType !== "イベント一覧") return "all";
    return ["ON", "OFF"].includes(widgetValue) ? widgetValue : "all";
  }

  window.EventLoggerQueryPolicy = Object.freeze({
    dashboardApiUrl, buildLogApiQuery, logValueFilterForWidget
  });
})();
