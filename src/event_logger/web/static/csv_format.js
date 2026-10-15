/* デモCSVの列・エスケープ仕様。実機のCSVは引き続きサーバーAPIが提供する。 */
(() => {
  "use strict";

  function csvCell(value) {
    let text = String(value ?? "");
    if (/^\s*[=+\-@]|^[\t\r\n]/.test(text)) text = `'${text}`;
    return `"${text.replaceAll('"', '""')}"`;
  }

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
