"use strict";

// 本体再起動は更新設定の有無に依存させず、常設の確認ダイアログを通す。
(function mountRestartPanel() {
  const button = document.getElementById("restart-system");
  const dialog = document.getElementById("restart-dialog");
  const status = document.getElementById("restart-status");
  if (!button || !dialog || !status) return;
  let pending = false;
  button.addEventListener("click", () => {
    if (pending || dialog.open) return;
    dialog.returnValue = "";
    button.disabled = true;
    dialog.showModal();
  });
  dialog.addEventListener("close", async () => {
    if (dialog.returnValue !== "confirm") {
      button.disabled = false;
      return;
    }
    if (pending) return;
    if (window.EVENT_LOGGER_DEMO || !window.EVENT_LOGGER_LIVE) {
      status.textContent = "デモ表示です。本体の再起動は行いません。";
      button.disabled = false;
      return;
    }
    pending = true;
    status.textContent = "本体の再起動を依頼しています…";
    try {
      const token = document.querySelector('meta[name="csrf-token"]')?.content || "";
      const response = await fetch("/api/system/reboot", {
        method: "POST",
        credentials: "same-origin",
        headers: { "X-CSRF-Token": token, Accept: "application/json" },
      });
      const payload = await response.json();
      if (!response.ok || payload.ok !== true || payload.data?.started !== true) {
        throw new Error(payload.error?.message || "再起動を開始できませんでした。");
      }
      status.textContent = "再起動を依頼しました。1分ほど待ってから画面を再読み込みし、記録状態を確認してください。";
    } catch (error) {
      status.textContent = error instanceof Error ? error.message : "再起動を開始できませんでした。";
      pending = false;
      button.disabled = false;
    }
  });
})();
