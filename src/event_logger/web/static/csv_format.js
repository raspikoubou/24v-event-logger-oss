/* デモCSVの列・エスケープ仕様。実機のCSVは引き続きサーバーAPIが提供する。 */
(() => {
  "use strict";

  /**
   * CSV cellをquoteし、spreadsheet式としての評価を防ぐ。
   * @param {string} value - 変換対象の値。
   * @returns {string} CSV規則でquoteし、先頭式記号を持つcellにはapostropheを付けた文字列。
   */
  function csvCell(value) {
    let text = String(value ?? "");
    if (/^\s*[=+\-@]|^[\t\r\n]/.test(text)) text = `'${text}`;
    return `"${text.replaceAll('"', '""')}"`;
  }

  /**
   * 固定demo event列をUTF-8 CSV本文へ変換する。
   * @param {Array<Object>} rows - CSV record rows。
   * @param {Object} options - 日時・signal表示に使うformatter callback群。
   * @param {Function} options.formatLocalDateTime - ローカル日時を表示するformatter callback。
   * @param {Function} options.eventTimeMs - event時刻をmillisecondへ変換する関数。
   * @param {Function} options.signalDisplayName - 信号表示名変換関数。
   * @returns {string} header行とrecord行をCRLFで連結したdemo CSV本文。日時・信号名変換は引数callbackを使う。
   */
  function serializeDemoCsv(rows, { formatLocalDateTime, eventTimeMs, signalDisplayName }) {
    const header = "timestamp,signal,name,value,kind,source,clock_synced";
    const body = rows.map((event) => [
      formatLocalDateTime(eventTimeMs(event), true), event.signal, signalDisplayName(event.signal), event.value,
      event.kind, event.source, event.synced
    ].map(csvCell).join(","));
    return [header, ...body].join("\n");
  }

  window.EventLoggerCsvFormat = Object.freeze({ csvCell, serializeDemoCsv });
})();
