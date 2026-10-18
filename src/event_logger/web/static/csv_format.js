/* デモCSVの列・エスケープ仕様。実機のCSVは引き続きサーバーAPIが提供する。 */
(() => {
  "use strict";

  // CSVで数式として解釈され得る先頭文字を無害化し、引用符もエスケープする。
  function csvCell(value) {
    let text = String(value ?? "");
    if (/^\s*[=+\-@]|^[\t\r\n]/.test(text)) text = `'${text}`;
    return `"${text.replaceAll('"', '""')}"`;
  }

  // デモ用に画面上のイベントをCSVへ変換する。実機のCSVはAPI側で生成する。
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
