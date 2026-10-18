/* 記録保持期間のUI/API変換。無期限はnull、値未設定は既定の365日を維持する。 */
(() => {
  "use strict";

  // 「無期限」はAPI/DBのnullで表す。selectの空値をNumber()で0へ変換しない。
  /**
   * select値を有効な保存日数へ変換する。
   * @param {string} value - 変換対象の値。
   * @returns {number|null} selectの空値ならnull（無期限）、それ以外はNumber変換した日数。
   */
  function retentionDaysFromSelectValue(value) {
    return value === "" ? null : Number(value);
  }

  /**
   * status payloadから保存日数を読む。
   * @param {Object} status - 更新status。
   * @returns {number|null} status.retention_days。fieldが欠落した旧payloadでは365日。
   */
  function retentionDaysFromStatus(status) {
    return status.retention_days === undefined ? 365 : status.retention_days;
  }

  window.EventLoggerRetentionPolicy = Object.freeze({
    retentionDaysFromSelectValue,
    retentionDaysFromStatus
  });
})();
