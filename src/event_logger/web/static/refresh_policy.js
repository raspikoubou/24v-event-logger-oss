/* API更新の直列化と表示更新ポリシー。DOM・通信・デモfixtureには依存しない。 */
(() => {
  "use strict";

  // API応答が1秒を超えてもtimerの呼出しを積み上げず、必要なら最後の1回だけ追従する。
  // 並列取得の重複を避け、更新要求が競合して画面へ逆順に反映されることを防ぐ。
  function createRefreshCoordinator(refresh) {
    let activePromise = null;
    let forcedRerunRequested = false;

    return function requestRefresh(forceDashboard = false) {
      if (activePromise) {
        forcedRerunRequested ||= forceDashboard;
        return activePromise;
      }

      const runRefreshes = async () => {
        let forceNextDashboard = forceDashboard;
        try {
          do {
            forcedRerunRequested = false;
            await refresh(forceNextDashboard);
            forceNextDashboard = forcedRerunRequested;
          } while (forceNextDashboard);
        } finally {
          activePromise = null;
        }
      };

      activePromise = runRefreshes();
      return activePromise;
    };
  }

  // 指定期間・表示状態・再取得間隔から重いダッシュボード更新の要否を判断する。
  function shouldRefreshDashboard({
    currentPeriod,
    customRange,
    liveMode,
    forceDashboard,
    dashboardLoaded,
    hasCurrentStateWidgets = false,
    nextRefreshAtMs,
    nowMs
  }) {
    const validCustomRange = (
      Number.isInteger(customRange?.from_ms)
      && Number.isInteger(customRange?.to_ms)
      && customRange.from_ms >= 0
      && customRange.from_ms < customRange.to_ms
    );
    if (currentPeriod === "custom" && !validCustomRange) return false;
    const closedCustomRange = currentPeriod === "custom"
      && Number.isInteger(customRange?.to_ms)
      && customRange.to_ms <= nowMs;
    // 集計期間が過去でも現在状態の継続時間は通常の取得間隔で更新する。
    return liveMode && (
      forceDashboard
      || !dashboardLoaded
      || ((!closedCustomRange || hasCurrentStateWidgets) && nowMs >= nextRefreshAtMs)
    );
  }

  // 詳細表示やキーボード操作中はタイムラインDOMを作り直さない。
  function shouldDeferTimelineRender({ pinned, focused, detailVisible }) {
    return Boolean(pinned || focused || detailVisible);
  }

  // 個別ウィジェットのフォーカス・詳細表示をポーリングで破壊しない。
  function shouldDeferTimelineWidgetRender({
    containsPinnedEvent,
    containsFocusedEvent,
    containsHoveredEvent,
    containsDetailEvent
  }) {
    return Boolean(
      containsPinnedEvent
      || containsFocusedEvent
      || containsHoveredEvent
      || containsDetailEvent
    );
  }

  // ログ画面の表示中だけ必要なタイミングで一覧を更新する。
  function shouldRefreshLogPage({
    activeView,
    liveMode,
    logPageLoading,
    periodKey,
    rangeEndMs,
    nextRefreshAtMs,
    nowMs
  }) {
    const closedCustomRange = periodKey === "custom"
      && Number.isFinite(rangeEndMs)
      && rangeEndMs <= nowMs;
    return activeView === "logs"
      && liveMode
      && !logPageLoading
      && !closedCustomRange
      && nowMs >= nextRefreshAtMs;
  }

  window.EventLoggerRefreshPolicy = Object.freeze({
    createRefreshCoordinator,
    shouldRefreshDashboard,
    shouldDeferTimelineRender,
    shouldDeferTimelineWidgetRender,
    shouldRefreshLogPage
  });
})();
