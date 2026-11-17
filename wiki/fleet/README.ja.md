# 🏢 Fleet デプロイ：テナントごとに 1 つの Sherry cell

[**English**](README.md) · [中文](README.zh.md) · [한국어](README.ko.md) · **日本語**

> Fleet デプロイはテナントごとに Sherry バックエンドを 1 つ実行し、その前に単一の Ingress を置きます。各 cell は独自の `SHERRY_HOME`、workspace、SQLite ストアを持ち、Ingress がブラウザのログインを担当して認証済みトラフィックだけを転送します。同じインターフェースに 2 つのバックエンドがあります。信頼できるテナント向けのベアメタル **process** 層と、信頼できないテナント向けの **container** 層（Docker または Podman）で、後者は OS レベルのファイルシステム・ネットワーク・cgroup 分離を提供します。cell が公開インターフェースを待ち受けることはありません。

**情報源：** `fleet/`、`packaging/MULTI_TENANT.md`、`tests/fleet/`。

## 🎯 これは何か

デプロイは 3 層で構成されます。ブラウザは Ingress（`python -m fleet serve`、既定ポート 8081）だけに接続します。Ingress はユーザーを認証し、JWT claims からテナントを解決して、HTTP と WebSocket のトラフィックをそのテナントの cell へ逆プロキシします。cell は通常の `python -m server` プロセスか、同じサーバーイメージを実行するコンテナで、20100-20299 のポートで `127.0.0.1` にバインドされます。Fleet manager が cell 専用の gateway token を渡して起動します。テナントの状態は 1 つのディレクトリツリー（`SHERRY_HOME`、`workspace/`、`sherry.jsonc`、`logs/`）に収まり、どちらのバックエンドも同じツリーをマウントまたは参照するため、テナントを両者の間で移せます。

## 🏗️ トポロジ

```text
browser ──HTTP(S)──▶ Ingress :8081 ──loopback──▶ cell A  process  :20100
                             │                      SHERRY_HOME=<fleet_root>/a
                             ├──loopback──▶ cell B  container :20101
                             │                      /app/data ← <fleet_root>/b
                             └──loopback──▶ shared daemons :9012 :9013 :9014 :9015
```

Ingress とすべての cell は同じ OS ユーザーで実行されます。process 層は信頼できるテナント向けで、境界はプロセス状態・ポート・ルーティングであり、カーネルサンドボックスではありません。container 層は独立したマウント名前空間、専用の bridge ネットワーク、CPU・メモリ・pids の厳格な上限を追加します。

## 🚪 Ingress：認証とブラウザ契約

Ingress はすべての `/auth/*` ルートを所有し、`/auth/token` を逆プロキシしません。そのため cell が起動ごとに生成する gateway token がブラウザへ漏れることはありません。ログインは 2 つの HttpOnly cookie（`sherry_ingress_access`、`sherry_ingress_refresh`）を設定します。access token は短命の HS256 JWT で、`tenants`（既定値が先頭）、`tenant_id`、`role` を含みます。refresh token は使用のたびにローテーションし、サーバー側で失効できます。

共有 SPA は単一テナント cell と同じ契約を見ます。`/auth/status` は `username_required` も返し、`/auth/login` は `{username, password}` を受けて `expires_in` を返し、`/auth/me` は有効な refresh cookie から期限切れの access cookie を回復し、`/auth/account` はパスワードを変更します（有効化・無効化は管理者のみ）。安全でないメソッドは Origin と `Sec-Fetch-Site` の CSRF 検査を通ります。同一 `(client, username)` で 300 秒に 5 回失敗すると `429` を返します。

`SHERRY_INGRESS_TLS_CERT` と `SHERRY_INGRESS_TLS_KEY` を設定すると Ingress が TLS を終端し、両方の cookie を `Secure` にします。`SHERRY_INGRESS_ALLOWED_ORIGINS` は逆プロキシの追加オリジンです。静的アセットは公開され、`Accept: text/html` の GET は `200.html` または `index.html` を受け取るため、`/login` や深いルートはセッション確立前でも読み込めます。API 呼び出しは制限されたままです。

## 👥 ユーザー、信頼ドメイン、cell の切り替え

cell はユーザーアカウントではなく信頼ドメインです。任意の数のアカウントが同じ `tenant_id` を指し、その cell のセッション・メモリ・スキル・プロジェクトディレクトリを共有できます。資格情報は Ingress データベースにアカウントごとに保存され、一方のパスワード変更は他方に影響しません。1 つのアカウントが複数の cell に所属でき、多対多の `user_tenants` テーブルに保持されます（旧 `users.tenant_id` は既定値として残ります）。`init()` は `tenant_id` が `UNIQUE` だった旧 `users` テーブルを一度だけ再構築し、その後メンバーシップを埋めます。

ログインはアカウントの既定メンバーシップを選び、認証 cookie の隣に HttpOnly の `sherry_tenant` cookie を設定します。`GET /auth/tenants` は `role`、`is_default`、`cell_status` を含む全メンバーシップを返し、`POST /auth/tenant` は要求された `tenant_id` を実テーブルで検証して cookie を書き換えます。プロキシと WebSocket ルーターはリクエストごとにその cookie を読み、メンバーシップを再確認するため、管理者が cell を取り消すと即座に反映されます。古い cookie はアカウントの既定値へフォールバックします。SPA の `TenantSwitcher` は複数の cell を持つ場合だけ表示され、切り替え後はページを再読み込みします（キャッシュされたセッション・メモリ・ソケットは前の cell のものだからです）。

メンバーシップのないアカウントはログインできません（`no_tenant`）。アクセス管理には `user-tenant-add`、`user-tenants`、`user-tenant-remove` を使います。最初のメンバーシップが owner かつ既定です。`rm` は cell を削除しますがメンバーシップは残すため、先に取り消すか再割り当てしてください。

## 🧩 Cell のライフサイクル

`python -m fleet create --tenant alice` は `<fleet_root>/alice/{workspace,logs,sherry.jsonc}` を作成し、20100-20299 で最初の空きポートを確保します。ポート選択と登録書き込みはプロセスロックと fleet ルートの `flock` で直列化されるため、2 つの CLI プロセスが同じポートを登録することはありません。`--backend process`（既定）は `runtime=process` を記録し、`--backend container` は `runtime=container` とイメージ・コンテナ名を記録します。以後のコマンドはこの列を読むため、バックエンドを再度指定する必要はありません。

`start` は `SHERRY_HOME`、`SHERRY_PROJECT_DIR`、`API_PORT`、`SHERRY_GATEWAY_TOKEN`、`PUBLIC_BASE_URL`、`SHERRY_DAEMON_HOME`、`SHERRY_DAEMON_CLIENT_ONLY` を注入し、サーバーを detached で起動して `/health` を予算切れまでポーリングします。

`stop` は環境変数がまだその cell の `SHERRY_HOME` を指している PID にだけシグナルを送ります。再利用された PID や外部の PID には触れません。`create` は `--seed-config` も受け付け、ホストの `.models` と `.network` をコピーします（これらのファイルは資格情報を含むため既定では無効）。

## 📦 Container 層（OS レベル分離）

`python -m fleet create --tenant alice --backend container --image sherry-agent:dev` は同じデータツリーと同じ Ingress ルートを保ちますが、cell は独自のコンテナで動きます。

```text
--name sherry-cell-alice        --init          --restart unless-stopped
--cap-drop=ALL                  --security-opt no-new-privileges
--memory 4g --cpus 2            --pids-limit 1024
--network sherry-cell-alice-net --volume <fleet_root>/alice:/app/data
-p 127.0.0.1:20100:8080         --add-host host.docker.internal:host-gateway
--label sherry.fleet.tenant=alice --label sherry.fleet.attempt=<id>
```

0600 の env ファイルが `SHERRY_HOME=/app/data`、`SHERRY_PROJECT_DIR=/app/data/workspace`、gateway token、`API_PORT`、`SHERRY_AUTH_ENABLED=false`、`PUBLIC_BASE_URL`、`LOCAL_DAEMON_HOST=host.docker.internal` を運び、`create` が読み終えるとすぐ削除されます。イメージはリポジトリの Dockerfile そのままなので、`docker build` または `podman build` が唯一のイメージ手順です。

ヘルスチェックは `/health` に加えて attempt ラベルを照合します。ポートに応答しても `sherry.fleet.attempt` が今回のものでなければ残骸とみなすため、エンジンが再起動した古いコンテナが新しいコンテナを装うことはできません。

**2 つのネットワークモード。** `SHERRY_FLEET_CONTAINER_NETWORK=publish`（Linux と macOS の既定）はテナントごとに bridge ネットワークを作り、`127.0.0.1:<port>` を公開します。コンテナ同士は到達できません。`host` はランタイムのネットワーク名前空間を共有し、cell は割り当てられたポートを直接待ち受けます。これは WSL2 のフォールバックで、WSL2 カーネルは nftables を備えず netavark が公開ルールを適用できないためです（`auto` は Windows ホストで `host`、それ以外で `publish` を選びます）。host モードはネットワーク分離と引き換えに移植性を得ます。カーネルが許す限り `publish` を保ってください。

**cell 自身のログインは構造上オフです。** Ingress からのトラフィックは loopback ではなく bridge ゲートウェイから届くため、cell が自身のログインを強制するとすべてのプロキシ要求に `401` を返します。新しい cell は `SHERRY_AUTH_ENABLED=false` により `auth_settings.enabled` を 0 で初期化します。**バックアップから復元した** cell は有効なログインを持ち込む可能性があります。manager は `/auth/status` で検出し、`<data_dir>/src/data/auth.db` の行を直接オフにし、cell の 2 秒の状態キャッシュが切れるのを待ちます。それでも有効なら `create`/`start` は明確に失敗し、誰も到達できない cell を残しません。

**アップグレードと監査。** `python -m fleet upgrade --tenant alice --image <tag>` はコンテナを停止し、新しいイメージで同じデータツリーに対して再作成します。新しいイメージのヘルスチェックが失敗すれば以前のイメージへロールバックします。`python -m fleet doctor [--tenant alice]` は各コンテナ cell を検査します。コンテナの存在と実行、`cap-drop` がエンジンの主要ケーパビリティ集合を削除していること（podman は展開リスト、docker は `ALL` を報告）、`no-new-privileges`、メモリ・CPU・pids 上限が非ゼロ、fleet ラベルがテナントと一致、公開バインドが `127.0.0.1` のみまたは `host` モード、テナントネットワークに 1 つのコンテナだけ、データディレクトリが `0700`（POSIX）、`/health` が応答すること。いずれかが失敗するとコマンドは 1 で終了します。

## 🛡️ 監督とリソース制限

| 症状 | Supervisor の対応 |
| :--- | :--- |
| 記録された PID が消えた、または `SHERRY_HOME` が一致しない | 古い子プロセスを回収し、代替を起動する |
| プロセスは生きているが `/health` が 3 回連続で失敗 | `stop` してから `start` |
| 停止した cell が未回収のゾンビを残す | 代替がポートをバインドする前に `waitpid` で回収する |
| `logs/cell.log` が `SHERRY_FLEET_LOG_MAX_BYTES` を超える | 実行中の O_APPEND 記述子に安全な copy-truncate で `cell.log.1` にする |
| cell が `SHERRY_FLEET_CELL_MEMORY_MB` や `SHERRY_FLEET_CELL_NOFILE` を超える | exec ラッパーが `ulimit` を適用済み。CPU クォータは cgroup か systemd の責務 |
| コンテナ cell が exited または消失 | `start` が再作成して起動する（データツリーはそのまま） |
| コンテナ cell は動作中だが `/health` が 3 回連続で失敗 | `stop` してから `start` |

Supervisor は Ingress 内で動作し、`SHERRY_FLEET_SUPERVISE_INTERVAL_S` 秒ごとに稼働中の cell を検査します。`SHERRY_FLEET_SUPERVISE=0` はこのループを無効にし、運用者による管理に委ねます。

## 💾 バックアップと復元

`python -m fleet backup --tenant alice --out <dir>` は `logs/`、`-wal`、`-shm` を除いて cell ツリーをコピーし、各 `.db` を `sqlite3.Connection.backup()` でスナップショットするため、SQLite ファイルはトランザクション一貫性を持ちます。cell の稼働中、データベース以外のファイルはベストエフォートです。container 層は同じホスト側ツリーを使うため、バックアップと復元は変わりません。

`restore` は cell を停止し、バックアップをステージングし、現在のツリーを `<tenant>.pre-restore` に改名してからステージングしたコピーを差し替えます。`python -m fleet backup-db --out <dir>` は中央の `fleet.db`（ユーザー、refresh token、cell、プロセス行）をバックアップします。

## 🤝 共有モデル daemon

`python -m fleet daemons-start` は各ローカルモデル daemon（embed、reranker、ローカルチャット、ビジョン）の唯一のコピーを `<fleet_root>/daemons` の下で所有します。cell は `SHERRY_DAEMON_CLIENT_ONLY=1` で動作し、記録された pid に接続するだけで、2 つ目のコピーを起動せず、atexit kill も登録しません。`SHERRY_DAEMON_HOME` が各 cell を共有 pid ディレクトリへ向けます。

ローカルチャットとビジョンには `SHERRY_FLEET_AUXILIARY_MODEL_PATH`、`SHERRY_FLEET_VISION_MODEL_PATH`、`SHERRY_FLEET_VISION_MMPROJ_PATH` も必要です。これらがない場合、コマンドは決してロードできない daemon を起動する代わりに不足変数を報告します。daemon が停止していると、クライアントはプロセス内ローダーへフォールバックするため機能は維持されますが、cell ごとのメモリ共有は失われます。

コンテナ cell は `host.docker.internal` 経由でホストの daemon に到達するため、daemon はランタイムが到達できるアドレスで待ち受ける必要があります（ホスト側で `LOCAL_DAEMON_HOST=0.0.0.0`）。VM ベースのランタイムの host ネットワークモードでは、コンテナから見た「ホスト」は VM でありデスクトップではありません。ローカルモデルは process cell か Linux ホストに残してください。

## 🖥️ CLI

```bash
python -m fleet serve
python -m fleet user-add --username alice --password '...' --tenant alice
python -m fleet user-tenant-add --username alice --tenant team-b --role member
python -m fleet user-tenants --username alice
python -m fleet user-tenant-remove --username alice --tenant team-b
python -m fleet create --tenant alice                                  # process バックエンド
python -m fleet create --tenant bob --backend container --image sherry-agent:dev
python -m fleet start --tenant alice
python -m fleet stop --tenant alice
python -m fleet upgrade --tenant bob --image sherry-agent:next          # container のみ
python -m fleet doctor --tenant bob                                    # container 監査
python -m fleet list
python -m fleet backup --tenant alice --out /srv/backups/alice
python -m fleet restore --tenant alice --from /srv/backups/alice
python -m fleet backup-db --out /srv/backups/fleet
python -m fleet daemons-start
python -m fleet daemons-stop
python -m fleet daemons-status
```

## 🔐 セキュリティ境界

| 境界 | デプロイが保証する内容 |
| :--- | :--- |
| ブラウザ資格情報 | HttpOnly、SameSite=Lax cookie。`/auth/token` は Ingress では 404 |
| Cell 資格情報 | cell ごとの `SHERRY_GATEWAY_TOKEN`。ブラウザが渡した token と ticket は削除される |
| Cell ネットワーク | cell は `127.0.0.1` のみにバインドし、Ingress は loopback で到達する |
| テナントファイル（process） | `SHERRY_HOME` がデータツリーを分けるが、すべての cell が同じ OS ユーザーを共有する。悪意あるテナントには container 層が必要 |
| テナントファイル（container） | テナントツリー上の専用マウント名前空間、`cap-drop=ALL`、`no-new-privileges`、厳格なメモリ・CPU・pids 上限。`terminal` と `python_repl` は他テナントのデータを読めない |
| コンテナネットワーク | `publish` モードはテナントごとの bridge と loopback のみの公開ポートを与え、コンテナ同士は到達できない |
| リソース枯渇 | コンテナの OOM や pids ストームは自身の cgroup 内で終息し、Ingress や他の cell に影響しない |

- 同じ tenant のアカウントはそのセルのセッションとメモリを共有します。ユーザー単位の分離には別のテナントが必要です。
- コンテナ cell はコンテナ内部では自テナントを信頼します。境界はテナント対テナントであり、ユーザー対ユーザーではありません。

## 🧪 テスト

`tests/fleet/` はストア、Ingress の認証、プロキシ、WebSocket、TLS、manager のライフサイクル、共有デーモン、バックアップと復元を網羅します。`test_cell_e2e.py` は manager 経由で実際の `python -m server` cell を起動し、`/health`、PID の同一性、ログイン、HTTP プロキシ、正常な SIGTERM 終了を検証します。

container バックエンドは `test_container_manager.py`（記録するフェイクランタイム：コマンド形状、env ファイル、2 つのネットワークモード、ヘルス失敗時の後片付け、アップグレードのロールバック、doctor の指摘、監督ループ、復元されたログインの無効化）と、実際のエンジンに `create` を実行させるオプトインのスモークテスト `test_container_runtime_e2e.py` を追加します。

```bash
SHERRY_FLEET_CONTAINER_E2E=1 uv run --no-sync pytest tests/fleet/test_container_runtime_e2e.py
```

`pytest tests/fleet` とリポジトリの split gate が回帰コマンドです。クライアント認証には専用の store、ログインページ、ルートガード、アカウントパネルのテスト群があり、バックエンド全体の gate は unit、integration、regression を別プロセスで実行します。

## 📌 運用要件

- Ingress は systemd または supervisord で管理してください。内部 supervisor は cell だけを管理し、自身は管理しません。
- SPA は `VITE_API_BACK_URL=same-origin` で同一オリジンとしてビルドし、`PUBLIC_BASE_URL` をブラウザから到達できる Ingress の origin に設定します。
- 安定した `SHERRY_INGRESS_JWT_SECRET` を設定します。起動ごとにランダム生成すると、再起動で全 cookie が無効になります。
- TLS は Ingress または信頼済みの逆プロキシで終端し、追加の origin を `SHERRY_INGRESS_ALLOWED_ORIGINS` に列挙します。
- manager が担当しない制限には、cgroup または systemd の CPU クォータ、外部のバックアップや logrotate のタイマーを追加します。
- テナントごとにプロジェクトディレクトリをバインドします。未設定の場合、2 つのテナントが `ROOT_DIR/.sherry/coordination.db` を共有します。
- すべてのアカウントに少なくとも 1 つのメンバーシップを付与してください。メンバーシップがないログインは `no_tenant` を返します。
- container 層には Ingress ホスト上の Docker または Podman バイナリ（`SHERRY_FLEET_CONTAINER_RUNTIME`）、ビルド済みイメージ（`SHERRY_FLEET_IMAGE`、`SHERRY_FLEET_CONTAINER_PULL`）、イメージとは別にアイドル cell あたり約 1 GB のメモリが必要です。ホストの実際の容量に合わせて `SHERRY_FLEET_CONTAINER_MEMORY` / `_CPUS` / `_PIDS` を設定してください。
- POSIX ホストではコンテナは fleet ユーザーの uid で動作するため、`0700` のデータツリーは chown なしで書き込めます。別のランタイム uid を使う場合はイメージ側で `--user` を扱うか、rootless の subuid マッピングを設定してください。
