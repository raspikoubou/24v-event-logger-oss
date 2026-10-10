# systemd 配置

インストーラーは次のunitを `/etc/systemd/system/` へ配置し、`event-logger` 専用ユーザーと
`/var/lib/event-logger`、`/run/event-logger` を作成してから有効化する。

- `event-loggerd.service`: GPIO監視、DB writer、Heartbeat、ローカルIPC
- `event-logger-web.service`: Flask/GunicornのWeb UI。GPIOへ直接アクセスしない
- `event-logger-hostname.service`: root oneshotでDB保存済みhostnameをOSへ反映
- `event-logger-maintenance.timer`: 日次retentionとincremental vacuum
- `event-logger-update.service`: Webからの更新依頼をroot updaterへ固定引数で中継するagent

Webとloggerdは別serviceのため、Web停止中もloggerdは記録を継続する。
WebはGunicorn 1 worker / gthread / 4 threads / timeout 60秒で起動する。
各HTTP要求は独自のSQLite接続を持ち、CSVのstreaming応答が閉じてから接続を閉じる。

SQLite 3.53.4はOS共有libraryを置換せず、`/opt/event-logger/runtime/sqlite/`の
root-owned private runtimeからロードする。5つのSQLite使用serviceは共通runnerで起動し、
製品専用のsystemd drop-inも設定する。drop-inは旧releaseへのrollback時にもprivate runtimeを
維持するために使う。install/updateのbackup、migration、health CLIもrunner経由で実行する。
source archive、SHA-256、Pi AArch64 build recipe、第三者license noticeはreleaseに含める。
runtimeの切替に失敗しloader pathを反映できない場合は、SQLite未修正版を使うserviceを
再起動せず、停止状態で失敗を残す。

プロジェクト直下の `packaging/install.sh` は Raspberry Pi OS 上で専用ユーザー、専用venv、
release/current symlink、DB初期化、unit有効化を一度に行う。`uninstall.sh` は通常時にDBと
設定を残し、`--purge` の明示操作だけでデータ領域を削除する。

更新は `packaging/update.sh --archive ... --sha256 ... --version ...` を使う。
SHA-256検証、更新前SQLite backup、migration、health check、失敗時の旧release/DBへの
rollbackを一つの流れにする。Web userが置くassetはroot agentがGitHub Releaseのchecksumを
再取得して照合し、`/var/lib/event-logger-update/verified`（root専用）へコピーしてから
helperへ渡す。実際のGitHub Release運用とPi 4/Pi 5実機確認は別途必要である。
更新処理はserviceを止めた後、private SQLite runtimeを検証・固定してloader pathを
systemdへ反映し、それからDB backup/migrationへ進む。
