/* API更新の直列化と表示更新ポリシー。DOM・通信・デモfixtureには依存しない。 */
(() => {
  "use strict";

  // API応答が1秒を超えてもtimerの呼出しを積み上げず、必要なら最後の1回だけ追従する。
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

  function shouldRefreshDashboard({
    currentPeriod,
    customRange,
    liveMode,
    forceDashboard,
    dashboardLoaded,
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
    return liveMode && (
      forceDashboard
      || !dashboardLoaded
      || (!closedCustomRange && nowMs >= nextRefreshAtMs)
    );
  }

  function shouldDeferTimelineRender({ pinned, focused, detailVisible }) {
    return Boolean(pinned || focused || detailVisible);
  }

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
