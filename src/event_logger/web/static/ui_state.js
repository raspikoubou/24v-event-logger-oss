(() => {
  "use strict";

  /** Owns the mutable search and paging state for one log view. */
  function createLogController() {
    const state = {
      page: 1, events: [], totalCount: 0, hasNext: false, loaded: false,
      loading: false, searchRange: null, nextRefreshAtMs: 0, requestId: 0
    };
    return {
      get page() { return state.page; },
      get events() { return state.events; },
      get totalCount() { return state.totalCount; },
      get hasNext() { return state.hasNext; },
      get loaded() { return state.loaded; },
      get loading() { return state.loading; },
      get searchRange() { return state.searchRange; },
      get nextRefreshAtMs() { return state.nextRefreshAtMs; },
      setPage(value) { state.page = Math.max(1, value); },
      setSearchRange(value) { state.searchRange = value; },
      resetQuery() {
        state.requestId += 1;
        state.loading = false;
        state.page = 1;
        state.searchRange = null;
        state.loaded = false;
        state.events = [];
        state.totalCount = 0;
        state.hasNext = false;
      },
      acceptResponse(requestId, result, pageSize) {
        if (requestId !== state.requestId) return { stale: true, retry: false };
        state.searchRange = { fromMs: Number(result.from_ms), toMs: Number(result.to_ms) };
        state.totalCount = Number(result.total_count || 0);
        const pageCount = Math.max(1, Math.ceil(state.totalCount / pageSize));
        if (state.page > pageCount) {
          state.page = pageCount;
          return { stale: false, retry: true };
        }
        state.events = (result.items || []).map((event, index) => ({
          id: Number(event.id?.split(":").pop()) || index + 1,
          timestamp: event.timestamp,
          signal: event.signal,
          value: event.value,
          kind: event.kind,
          source: event.source || "",
          synced: Boolean(event.clock_synced)
        }));
        state.hasNext = Boolean(result.has_next);
        state.loaded = true;
        return { stale: false, retry: false };
      },
      finishRequest(requestId, nowMs) {
        if (requestId !== state.requestId) return false;
        state.loading = false;
        state.nextRefreshAtMs = nowMs + 5_000;
        return true;
      },
      async loadPage({ fetchPage, pageSize, now, onChange }) {
        const requestId = ++state.requestId;
        state.loading = true;
        onChange();
        try {
          let result;
          let accepted;
          do {
            result = await fetchPage(state.page, state.searchRange);
            if (requestId !== state.requestId) return;
            accepted = this.acceptResponse(requestId, result, pageSize());
          } while (accepted.retry);
        } finally {
          if (requestId === state.requestId) {
            state.loading = false;
            state.nextRefreshAtMs = now() + 5_000;
          }
          onChange();
        }
      }
    };
  }

  /** Keeps widget edit state separate from snapshots arriving during polling. */
  function createWidgetEditorState() {
    let editingRow = null;
    let dirty = false;
    return {
      get editingRow() { return editingRow; },
      set editingRow(row) { editingRow = row; },
      get dirty() { return dirty; },
      set dirty(value) { dirty = value; },
      setEditingRow(row) { editingRow = row; },
      markDirty() { dirty = true; },
      clearDirty() { dirty = false; },
      shouldApplyServerWidgets(dialogOpen) { return !dirty && !dialogOpen; }
    };
  }

  /** Serializes server snapshots and retains the newest applied snapshot. */
  function createLiveStateController(runRefresh) {
    let snapshot = null;
    let inFlight = null;
    let pendingForce = false;
    const refresh = (force = false) => {
      if (inFlight) {
        pendingForce = pendingForce || force;
        return inFlight;
      }
      inFlight = (async () => {
        let forceNext = force;
        try {
          do {
            pendingForce = false;
            const next = await runRefresh(forceNext);
            if (next) snapshot = next;
            forceNext = pendingForce;
          } while (forceNext);
        } finally {
          pendingForce = false;
        }
      })().finally(() => { inFlight = null; });
      return inFlight;
    };
    return {
      refresh,
      acceptSnapshot(value) { snapshot = value; },
      get snapshot() { return snapshot; }
    };
  }

  window.EventLoggerUiState = { createLogController, createWidgetEditorState, createLiveStateController };
})();
