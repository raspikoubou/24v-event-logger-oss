/* HTML表示用のエスケープ、信号選択、チャンネル名の整形。DOMには依存しない。 */
(() => {
  "use strict";

  // 複数信号の選択値を表示処理で扱いやすい配列へ変換する。
  function signalIds(value) {
    return String(value || "").split(",").filter(Boolean);
  }

  // 有効状態と任意の設備名を入力チャンネルの表示名へ反映する。
  function channelLabel(channel) {
    const name = channel.name.trim();
    return `IN${channel.id}${name ? ` ${name}` : ""}${channel.enabled ? "" : "（未使用）"}`;
  }

  // イベント名など外部由来の文字列をHTMLへ埋め込む前にエスケープする。
  function escapeHtml(value) {
    return String(value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  window.EventLoggerViewFormat = Object.freeze({
    signalIds,
    channelLabel,
    escapeHtml
  });
})();
