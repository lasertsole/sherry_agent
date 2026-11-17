# 🏢 ベアメタル Fleet：テナントごとに 1 つの Sherry プロセス

[**English**](README.md) · [中文](README.zh.md) · [한국어](README.ko.md) · **日本語**

> Fleet デプロイはテナントごとに通常の Sherry バックエンドを 1 つ実行し、その前に単一の Ingress を置きます。各 cell は独自の `SHERRY_HOME`、workspace、SQLite ストアを持ち、Ingress がブラウザのログインを担当して認証済みトラフィックだけを転送します。Docker は不要で、cell が公開インターフェースを待ち受けることはありません。

**情報源：** `fleet/`、`packaging/MULTI_TENANT.md`、`tests/fleet/`。

## 🎯 これは何か

デプロイは 3 層で構成されます。ブラウザは Ingress（`python -m fleet serve`、既定ポート 8081）だけに接続します。Ingress はユーザーを認証し、JWT claims からテナントを解決して、HTTP と WebSocket のトラフィックをそのテナントの cell へ逆プロキシします。cell は通常の `python -m server` プロセスで、20100-20299 のポートで `127.0.0.1` にバインドされます。Fleet manager が cell 専用の gateway token を渡し、detached で起動します。テナントの状態は 1 つのディレクトリツリーに収まります：`SHERRY_HOME`、`workspace/`、`sherry.jsonc`、`logs/`。

## 🏗️ トポロジー

```text
browser ──HTTP(S)──▶ Ingress :8081 ──loopback──▶ cell A  python -m server :20100
                             │                      SHERRY_HOME=<fleet_root>/a
                             ├──loopback──▶ cell B  python -m server :20101
                             │                      SHERRY_HOME=<fleet_root>/b
                             └──loopback──▶ shared daemons :9012 :9013 :9014 :9015
```

Ingress とすべての cell は同じ OS ユーザーで動作します。これは信頼済みテナント層であり、境界はプロセス状態、ポート、ルーティングであって、カーネルサンドボックスではありません。

## 🚪 Ingress：認証とブラウザ契約

Ingress はすべての `/auth/*` ルートを所有し、`/auth/token` を決してプロキシしません。したがって cell の起動ごとの gateway token がブラウザへ漏れることはありません。ログインは 2 つの HttpOnly cookie（`sherry_ingress_access`、`sherry_ingress_refresh`）を設定します。access token は `tenant_id` と `role` を持つ短命の HS256 JWT で、refresh token は使用のたびにローテーションし、サーバー側で失効できます。

共有 SPA が見る契約は単一テナントの cell と同じです。`/auth/status` はさらに `username_required` を返し、`/auth/login` は `{username, password}` を受け取って `expires_in` を返します。`/auth/me` は有効な refresh cookie から期限切れの access cookie を回復し、`/auth/account` はパスワードを変更します（ログインの有効化・無効化は管理者のみ）。安全でないメソッドのリクエストは Origin と `Sec-Fetch-Site` の CSRF 検査を通ります。同じ `(client, username)` で 300 秒以内に 5 回失敗すると `429` を返します。

`SHERRY_INGRESS_TLS_CERT` と `SHERRY_INGRESS_TLS_KEY` を設定すると、Ingress が TLS を終端し、両方の cookie に `Secure` を付けます。`SHERRY_INGRESS_ALLOWED_ORIGINS` は逆プロキシの origin を追加します。静的アセットは公開され、`Accept: text/html` を持つすべての GET は `200.html` または `index.html` を受け取るため、`/login` と深いルートはセッション確立前でも読み込めます。API 呼び出しは引き続き認証が必要です。

## 🧩 Cell のライフサイクル

`python -m fleet create --tenant alice` は `<fleet_root>/alice/{workspace,logs,sherry.jsonc}` を作成し、20100-20299 の最初の空きポートを割り当てます。ポート選択とレジストリ書き込みはプロセス内ロックと fleet ルートの `flock` で直列化されるため、2 つの CLI プロセスが同じポートを登録することはありません。`start` は `SHERRY_HOME`、`SHERRY_PROJECT_DIR`、`API_PORT`、`SHERRY_GATEWAY_TOKEN`、`PUBLIC_BASE_URL`、`SHERRY_DAEMON_HOME`、`SHERRY_DAEMON_CLIENT_ONLY` を注入し、サーバーを detached で起動して、予算が尽きるまで `/health` をポーリングします。

`stop` は環境変数がまだその cell の `SHERRY_HOME` を指している PID にだけシグナルを送ります。再利用された PID や外部の PID には触れません。`create` は `--seed-config` も受け付け、ホストの `.models` と `.network` をコピーします（これらのファイルは資格情報を含むため既定では無効）。

## 🛡️ 監視とリソース制限

| 症状 | Supervisor の対応 |
| :--- | :--- |
| 記録された PID が消えた、または `SHERRY_HOME` が一致しない | 古い子プロセスを回収し、代替を起動する |
| プロセスは生きているが `/health` が 3 回連続で失敗する | `stop` してから `start` する |
| 停止した cell が未回収の zombie を残した | 代替がポートをバインドする前に `waitpid` で回収する |
| `logs/cell.log` が `SHERRY_FLEET_LOG_MAX_BYTES` を超えた | `cell.log.1` へ copy-truncate する。実行中の O_APPEND 記述子に安全 |
| cell が `SHERRY_FLEET_CELL_MEMORY_MB` または `SHERRY_FLEET_CELL_NOFILE` を超えた | exec ラッパーが `ulimit` を適用済み。CPU クォータは cgroup または systemd の責務 |

Supervisor は Ingress 内で動作し、`SHERRY_FLEET_SUPERVISE_INTERVAL_S` 秒ごとに各 running cell を検査します。`SHERRY_FLEET_SUPERVISE=0` はこのループを無効化し、外部運用に任せます。

## 💾 バックアップと復元

`python -m fleet backup --tenant alice --out <dir>` は cell ツリーをコピーし、`logs/`、`-wal`、`-shm` をスキップします。各 `.db` は `sqlite3.Connection.backup()` でスナップショットを取るため、SQLite ファイルはトランザクション整合性を保ちます。cell の実行中、データベース以外のファイルは best-effort です。

`restore` は cell を停止し、バックアップを staging に展開し、現在のツリーを `<tenant>.pre-restore` に改名してから staging を入れ替えます。`python -m fleet backup-db --out <dir>` は中央の `fleet.db`（ユーザー、refresh token、cell、プロセス行）をバックアップします。

## 🤝 共有モデルデーモン

`python -m fleet daemons-start` は `<fleet_root>/daemons` の下で、各ローカルモデルデーモン（embed、reranker、local chat、vision）の唯一のコピーを所有します。cell は `SHERRY_DAEMON_CLIENT_ONLY=1` で動作し、記録された pid に接続します。2 つ目のコピーを起動せず、atexit kill も登録しません。`SHERRY_DAEMON_HOME` はすべての cell を共有 pid ディレクトリへ向けます。

Local chat と vision には `SHERRY_FLEET_AUXILIARY_MODEL_PATH`、`SHERRY_FLEET_VISION_MODEL_PATH`、`SHERRY_FLEET_VISION_MMPROJ_PATH` も必要です。これらがない場合、コマンドは決して読み込めないデーモンを起動せず、不足している変数を報告します。デーモンが停止している場合、クライアントはプロセス内ローダーへフォールバックするため、機能は維持されますが cell ごとのメモリ共有は失われます。

## 🖥️ CLI

```bash
python -m fleet serve
python -m fleet user-add --username alice --password '...' --tenant alice
python -m fleet create --tenant alice
python -m fleet start --tenant alice
python -m fleet stop --tenant alice
python -m fleet list
python -m fleet backup --tenant alice --out /srv/backups/alice
python -m fleet restore --tenant alice --from /srv/backups/alice
python -m fleet backup-db --out /srv/backups/fleet
python -m fleet daemons-start
python -m fleet daemons-stop
python -m fleet daemons-status
```

## 🔐 セキュリティ境界

| 境界 | デプロイが保証すること |
| :--- | :--- |
| ブラウザ資格情報 | HttpOnly、SameSite=Lax cookie。`/auth/token` は Ingress で 404 |
| Cell 資格情報 | cell ごとの `SHERRY_GATEWAY_TOKEN`。ブラウザが送った token と ticket は削除される |
| Cell ネットワーク | cell は `127.0.0.1` だけにバインドし、Ingress は loopback 経由で到達する |
| テナントファイル | `SHERRY_HOME` はデータツリーを分離するが、すべての cell は 1 つの OS ユーザーを共有する。悪意あるテナントには P3 のコンテナ層が必要 |

## 🧪 テスト

`tests/fleet/` はストア、Ingress の認証、プロキシ、WebSocket、TLS、manager のライフサイクル、共有デーモン、バックアップと復元を網羅します。`test_cell_e2e.py` は manager 経由で実際の `python -m server` cell を起動し、`/health`、PID の同一性、ログイン、HTTP プロキシ、正常な SIGTERM 終了を検証します。

`pytest tests/fleet` とリポジトリの split gate が回帰コマンドです。クライアント認証には専用の store、ログインページ、ルートガード、アカウントパネルのテスト群があり、バックエンド全体の gate は unit、integration、regression を別プロセスで実行します。

## 📌 運用要件

- Ingress は systemd または supervisord で管理してください。内部 supervisor は cell だけを管理し、自身は管理しません。
- SPA は `VITE_API_BACK_URL=same-origin` で同一オリジンとしてビルドし、`PUBLIC_BASE_URL` をブラウザから到達できる Ingress の origin に設定します。
- 安定した `SHERRY_INGRESS_JWT_SECRET` を設定します。起動ごとにランダム生成すると、再起動で全 cookie が無効になります。
- TLS は Ingress または信頼済みの逆プロキシで終端し、追加の origin を `SHERRY_INGRESS_ALLOWED_ORIGINS` に列挙します。
- manager が担当しない制限には、cgroup または systemd の CPU クォータ、外部のバックアップや logrotate のタイマーを追加します。
- テナントごとにプロジェクトディレクトリをバインドします。未設定の場合、2 つのテナントが `ROOT_DIR/.sherry/coordination.db` を共有します。
