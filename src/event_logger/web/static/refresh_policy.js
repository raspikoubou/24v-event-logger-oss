/* API更新の直列化と表示更新ポリシー。DOM・通信・デモfixtureには依存しない。 */
(() => {
  "use strict";

  // API応答が1秒を超えてもtimerの呼出しを積み上げず、必要なら最後の1回だけ追従する。
  /**
   * 重複refreshを抑えて更新処理を調整するcontrollerを作る。
   * @param {Function} refresh - refresh関数。
   * @returns {Object} 一度に一つのrefreshを実行し、重複要求を次回実行へまとめるrunRefreshesを含むobject。
   */
  function createRefreshCoordinator(refresh) {
    let activePromise = null;
    let forcedRerunRequested = false;

    return function requestRefresh(forceDashboard = false) {
      if (activePromise) {
        forcedRerunRequested ||= forceDashboard;
        return activePromise;
      }

      /**
       * 必要なdashboard、log refreshを順序立てて実行する。
       * @returns {Promise<void>} 保留中refresh callbackが完了した時にresolveする。
       */
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

  /**
   * 現在条件でdashboardを更新すべきか判定する。
   * @param {Object} options - Dashboard更新期限、表示期間、live mode、現在状態widgetの判定材料。
   * @param {string} options.currentPeriod - 現在選択中の期間。
   * @param {Object|null} options.customRange - 任意期間の境界値。
   * @param {string} options.liveMode - live更新中か。
   * @param {boolean} options.forceDashboard - dashboard更新を強制するか。
   * @param {string} options.dashboardLoaded - dashboardデータ取得済みか。
   * @param {boolean} options.hasCurrentStateWidgets - 現在状態widgetが存在するか。
   * @param {number} options.nextRefreshAtMs - 次回更新時刻（Unix epoch millisecond）。
   * @param {number} options.nowMs - 現在時刻（Unix epoch millisecond）。
   * @returns {boolean} 更新期限またはforce/current-state条件によりDashboardを更新する場合true。
   */
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

  /**
   * popup操作中のtimeline描画を遅らせるべきか判定する。
   * @param {Object} options - timeline detailの固定、keyboard focus、表示中状態。
   * @param {boolean} options.pinned - 詳細を固定表示しているか。
   * @param {boolean} options.focused - 対象eventへkeyboard focusがあるか。
   * @param {string} options.detailVisible - 詳細popupが表示中か。
   * @returns {boolean} pinned/focused/表示中detailがあるため描画を保留すべき時true。
   */
  function shouldDeferTimelineRender({ pinned, focused, detailVisible }) {
    return Boolean(pinned || focused || detailVisible);
  }

  /**
   * 対象widget上で操作中のため描画を保留すべきか判定する。
   * @param {Object} options - widget内のpinned/focused/hovered/detail eventの有無。
   * @param {string} options.containsPinnedEvent - widget内に固定中のeventがあるか。
   * @param {string} options.containsFocusedEvent - widget内にfocus中のeventがあるか。
   * @param {string} options.containsHoveredEvent - widget内にpointer hover中のeventがあるか。
   * @param {string} options.containsDetailEvent - widget内に詳細表示中のeventがあるか。
   * @returns {boolean} widget内のpinned/focused/hovered/detail eventを保持するため描画を保留する時true。
   */
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

  /**
   * 現在のviewと検索期間でlog pageを更新すべきか判定する。
   * @param {Object} options - log viewのactive状態、読込状態、期間終端、次回更新時刻。
   * @param {string} options.activeView - 現在のview key。
   * @param {string} options.liveMode - live更新中か。
   * @param {string} options.logPageLoading - log page読込中か。
   * @param {string} options.periodKey - 期間key。
   * @param {number} options.rangeEndMs - 検索期間終端（Unix epoch millisecond）。
   * @param {number} options.nextRefreshAtMs - 次回更新時刻（Unix epoch millisecond）。
   * @param {number} options.nowMs - 現在時刻（Unix epoch millisecond）。
   * @returns {boolean} log viewがactiveで更新期限に達し、pageを取得中でない場合true。
   */
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
