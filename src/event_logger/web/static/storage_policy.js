/* ストレージの警告判定・表示文生成。DOM・通信・loggerd制御とは独立。 */
(() => {
  "use strict";

  /**
   * byte数を人が読める単位文字列へ変換する。
   * @param {string} value - 変換対象の値。
   * @returns {string} byte数をKB/MB/GBへ換算した表示文字列。KB未満もKBとして表示し、MB/GBは小数1桁に丸める。
   */
  function formatBytes(value) {
    const bytes = Number(value || 0);
    if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
    if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
    return `${(bytes / 1024 / 1024 / 1024).toFixed(1)} GB`;
  }

  /**
   * storage statusから警告表示条件を判定する。
   * @param {Object} storage - storage status。
   * @param {boolean} hasError - 読み込みエラー有無。
   * @returns {Object} visible/severity/title/freeBytesを持つ警告情報。実測0 byteと未計測nullを区別し、取得errorはcriticalとして扱う。
   */
  function storageWarningFor(storage, hasError = false) {
    const status = storage?.status;
    if (!hasError && status !== "warning" && status !== "critical") {
      return { visible: false, severity: "", title: "", freeBytes: null };
    }
    const severity = hasError || status === "critical" ? "critical" : "warning";
    const rawFreeBytes = storage?.free_bytes;
    // nullや空文字は未計測値であり、0バイトの実測とは区別する。
    const hasMeasurement = (
      (typeof rawFreeBytes === "number"
        || (typeof rawFreeBytes === "string" && rawFreeBytes.trim() !== ""))
      && Number.isFinite(Number(rawFreeBytes))
    );
    return {
      visible: true,
      severity,
      title: hasError
        ? "ストレージ状態を確認できません"
        : severity === "critical"
          ? "ストレージ容量が不足しています"
          : "ストレージの空き容量が少なくなっています",
      freeBytes: !hasError && hasMeasurement
        ? Math.max(0, Number(rawFreeBytes))
        : null
    };
  }

  /**
   * storage warningを利用者向け説明へ変換する。
   * @param {Object} warning - warning state。
   * @returns {string} warning severityとfreeBytesに応じた利用者向け容量説明。
   */
  function storageWarningMessage(warning) {
    if (warning.severity === "critical") {
      const remaining = warning.freeBytes === null
        ? "空き容量を確認してください。"
        : `残り ${formatBytes(warning.freeBytes)}です。`;
      return `ログ保存を継続できなくなる可能性があります。${remaining}`;
    }
    const remaining = warning.freeBytes === null
      ? "空き容量を確認してください。"
      : `残り ${formatBytes(warning.freeBytes)}です。`;
    return remaining + "空き容量が30%未満です。診断画面で確認してください。";
  }

  /**
   * 警告内容と表示制御から警告領域の表示状態を決める。
   * @param {Object} options - latency/configuration warning件数とstorage warning表示状態。
   * @param {number} options.count - 件数。
   * @param {boolean} options.latencyWarning - 遅延warning有無。
   * @param {boolean} options.configurationWarning - 設定warning有無。
   * @param {boolean} options.storageVisible - storage warning表示状態。
   * @returns {Object} syncVisibleとvisibleを持つ表示判定。未解消のsync警告またはstorage warningがあればvisibleをtrueにする。
   */
  function warningVisibilityFor({
    count,
    latencyWarning,
    configurationWarning,
    storageVisible
  }) {
    const syncVisible = count > 0 || Boolean(latencyWarning) || Boolean(configurationWarning);
    return { syncVisible, visible: syncVisible || Boolean(storageVisible) };
  }

  window.EventLoggerStoragePolicy = Object.freeze({
    formatBytes,
    storageWarningFor,
    storageWarningMessage,
    warningVisibilityFor
  });
})();
