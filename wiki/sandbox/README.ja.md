# 🛡️ ツールサンドボックス: terminal、python_repl、PTC

[English](README.md) · [中文](README.zh.md) · [한국어](README.ko.md) · **日本語**

> エージェントがモデル発のコード実行をどう制約するか: すべての子プロセス生成時に環境変数を無条件に洗浄し、利用可能なら OS ネイティブのサンドボックスで包み、意図的なバイパスには人間の承認ゲートを置きます。

3つのツールがモデルにあなたのマシン上でのコード実行を許しています: `terminal`(シェルコマンド)、`python_repl`(子プロセス内の Python)、そして PTC の `execute_code`(RPC ブリッジ経由で実ツールを呼べる、子プロセス内のより長い Python スクリプト)です。幻覚やプロンプトインジェクションによる1つのコマンドが、環境変数から API キーを読み取ったり、プロジェクト外に書き込んだり、他のプロセスに触れたりできてしまいます。サンドボックス層はこの3つすべてを制限します。

事実の基準(source of truth): `agent/tools/pub_base/env_scrub.py`、`agent/tools/pub_base/sandbox.py`、`agent/tools/pub_base/sandbox_bwrap.py`、`agent/tools/pub_base/sandbox_seatbelt.py`、`agent/tools/pub_base/sandbox_win.py`、`agent/tools/pub_base/shell_resolver.py`、`agent/tools/pub_base/path_utils.py`、`agent/tools/file_tools/`、`agent/tools/terminal/core.py`、`agent/tools/python_repl.py`、`agent/tools/ptc/runner.py`、`agent/middlewares/humanInTheLoop/`、`agent/middlewares/path_guard/`。

## 目次

- [概要と脅威モデル](#-概要と脅威モデル)
- [分離機能と優先順位](isolation/README.ja.md)
- [landlock フォールバック](#landlock-フォールバックlinuxbwrap-なし)
  - [🧱 分離機能](isolation/README.ja.md#-分離機能)
  - [📊 優先順位マトリクス](isolation/README.ja.md#-優先順位マトリクス)
- [実装とアーキテクチャ](#%EF%B8%8F-実装とアーキテクチャ)
- [設定と使い方](#%EF%B8%8F-設定と使い方)
- [テスト](#-テスト)
- [正直な制限事項](#%EF%B8%8F-正直な制限事項)

## 🎯 概要と脅威モデル

| 露出面 | サンドボックスがない場合 | 防御 |
| :----- | :----------------------- | :--- |
| **環境変数内のシークレット** | 子プロセスが `*_API_KEY` を含む全変数を継承 | L1 環境変数洗浄 |
| **ファイルシステムへの書き込み** | 子がエージェントユーザーの書けるどこへでも記録 | L2 OS サンドボックス：Windows は AppContainer、Linux / macOS は bwrap / Seatbelt |
| **ファイルシステムからの読み取り** | 子が `~/.ssh`、`.env`、各種クレデンシャルストアを読める | L2 リードシールド(機密パスのマスク、Linux / macOS。Windows は AppContainer のパッケージ ID で分離) + terminal の機密ファイル正規表現 |
| **ファイルツールのパス引数** | ツール呼び出しが `read_file` にトラバーサルやハード拒否パスを要求 | §5 外部パスゲート + §6 3つの構造ゲート + §7 `PathGuard`(外部パスは依然 HITL 経由) |
| **シェルコマンドの引数** | `terminal(commands=…)` / `python_repl(query=…)` が資格情報ファイルを名指しする — これらはパス引数ではないため、パスゲートには見えない | §7 `PathGuard` のコマンドスクリーン(ハード拒否リストのみ。その他の外部パスは OS サンドボックスの担当) |
| **プロセス / セッションスコープ** | 子が名前空間を共有し、親より長く生き残り得る | L2 `--unshare-all`、`--die-with-parent`; Windows は Job Object(`ActiveProcessLimit`、`JobMemoryLimit`、`KILL_ON_JOB_CLOSE`) |
| **意図的なバイパス** | サーバー側の呼び出し元が `sandbox=False` を要求 | 人間の承認ゲート (HITL) —— `full_access` のセッションではスキップ。LLM スキーマはこのフラグを公開しない |
| **クラス内省による脱出** | `().__class__.__base__.__subclasses__()` が `open`/`__import__` なしで `os`/`subprocess` に到達 | AST ガード(`pub_base/safe_exec.py`)が子プロセス起動前に dunder 属性アクセスを拒否——**リテラルの `str.format`/`format_map` フィールド**経由の dunder(`"{0.__class__}".format(x)`)も含む。非リテラルのテンプレートは文字列しか返さないため許可 |
| **プログラム的ツール呼び出し (PTC)** | 生成された `execute_code` 子スクリプトが実ツールを呼び、ファイルシステムに直接到達し得る | L1 環境変数洗浄 + L2 OS サンドボックスラップ(`terminal` / `python_repl` と同じバックエンド)+ 制限付き builtins(`getattr`/`type`/`dir`/`vars` なし)+ インポート許可リスト + 同じ AST ガード |

2つの層と1つのゲート — それに加えてファイルツール独自のパス防御スタック:

- **L1. 環境変数洗浄**(`scrub_env`): 無条件、すべての生成時点で実行。人間が `sandbox=False` を承認した場合でも例外なし。機密名の変数を除去するだけでなく、**起動フック変数**(`PYTHONPATH`、`BASH_ENV`、`ENV` など)を自身の許可リストより高い優先度でブロックし、親環境を設定した者に選ばれたスクリプトの読み込みやモジュールの import を子プロセスに許しません。ローダー変数(`LD_PRELOAD`、`LD_LIBRARY_PATH` など)は `SHERRY_STRICT_ENV_HIJACK=1` のときだけブロックします——コンテナランタイムが実際に設定し、除去するとすべての子プロセスが起動しなくなるためです。
- **L2. OS ネイティブサンドボックス**: Linux は bubblewrap、macOS は Seatbelt、Windows は **AppContainer**(ユーザーごとのパッケージ SID が許可された書き込みルートだけに到達できる) + **制限トークン**(全特権を除去) + **Job Object**(プロセス/メモリ上限、kill-on-close)——書き込み封じ込め、読み取り分離、任意のネットワーク遮断([§2](isolation/README.ja.md#2-os-ネイティブサンドボックスバックエンド-l2)参照)。
- **人間の承認ゲート**: サーバー側の `sandbox=False` によるバイパスはメインセッションでのみ可能で、HITL インタラプトを通ります。モデルは要求できません: `terminal` と `python_repl` の呼び出しスキーマに `sandbox` フィールドはなく(2026-11-09 監査 H2)、ポリシーはサーバーが保持します。
- **PTC (`execute_code`)**: executor 専用で `sandbox` フラグを持たないため、常にサンドボックスを要求します — 子 argv は同じ L2 バックエンドで包まれます。`SANDBOX_POLICY=required` と `auto` はバックエンドがない場合に実行を拒否します——PTC には HITL 経路がないため降格しません（明示的な `off` のときだけサンドボックスなしで実行。 [分離 §2](isolation/README.ja.md#2-os-ネイティブサンドボックスバックエンド-l2)参照）。
- **ファイルツールのパスゲート**([分離 §5–§7](isolation/README.ja.md#5-外部ファイルパスゲートファイルツール)): `resolve_project_path()` の3つの構造ゲートと `O_NOFOLLOW` I/O、仮想パス描画、検索コンテインメント、6段階の外部パス承認フロー、そして `PathGuard` ミドルウェアのスクリーニング。

## ⚙️ 実装とアーキテクチャ

### ポリシー: `SandboxPolicy`

`SANDBOX_POLICY` 環境変数からパースされる3つの状態:

| 値 | 意味 |
| :- | :--- |
| `required`（推奨デプロイ値。同梱 `.env` は `auto`） | バックエンド利用不可 ⇒ コマンドを拒否、サンドボックスなしでは決して実行しない |
| `auto` | バックエンド利用不可 ⇒ メインセッションの対話呼び出しはサンドボックス迂回 HITL カードで承認を求める（ヘッドレスは自動拒否、confirm-all のカードには注記）。サブエージェント/バックグラウンド呼び出しと PTC は拒否（HITL 経路なし）。明示的な `off` のときだけサンドボックスなしで実行 |
| `off` | サンドボックス完全無効 |

環境変数が未設定のときの `read_policy()` のフォールバックは依然 `auto` です（稼働中のインストールは自分の設定を維持します）。一方、同梱 `.env` の値は `auto` です。デプロイでは `required` を明示してください — バックエンドが動かないホストでは `auto` がすべての対話的シェルコマンドを承認カード経由にし（ヘッドレスは拒否）、PTC は実行自体を拒否するからです（HITL 経路なし）。これは仮定ではなく実測です：**カーネルが user namespace をサポートしない**ホスト（Ubuntu 24.04 の既定 AppArmor ポリシー、PRoot/コンテナホスト）では `bwrap` バイナリは存在しても機能しません。起動時に実際のポリシーとバックエンドを出力し（`sandbox: policy=… backend=… available=…`）、`required` が満たされない場合は ERROR と修復コマンドを出し、`SANDBOX_STRICT_BOOT=1` なら起動自体を拒否します。実行中は `GET /sandbox/status` が同じ状態を返します。

`parse_policy` は空白を除去し大小文字を無視してマッチし、未知の値には `ValueError` を送出します: 誤入力された安全設定は大きな音で失敗すべきで、黙ってデフォルトにフォールバックしてはいけません。`read_policy()` は**毎回** `os.getenv` を呼びます(インポート時キャッシュなし)。そのためランタイムの変更が即座に反映されます。

### バックエンド契約とディスパッチ

`SandboxBackend` はすべてのバックエンドが実装する ABC です:

- `probe() -> bool`: 決して例外を送出してはいけません。バックエンドが自身のプローブ例外を捕捉して `False` を返します。
- `wrap(cmd, env, writable_roots) -> (argv, env)`: 包まれた argv と env を返し、list 形式で直接 exec されます(シェルなし)。`writable_roots` はそのコマンドが得る**唯一**の書き込み面です（呼び出し元セッションのプロジェクトディレクトリに加え、`terminal` / `python_repl` はそのセッション自身の `SESSIONS_DIR/<session_id>` ツリー — plans、attachments、uploads、`evicted/` プレビュー、scratch ファイル — を追加します。共有の正規化関数が `TEMP_DIR` を追加します — サンドボックス自身が書き込み可能な一時領域を必要とするため）。この引数は**必須**で、空リストは `ValueError` を送出します。デフォルト値を与えると、渡し忘れた呼び出し点が「`ROOT_DIR` 全体が書き込み可能」という旧挙動へ静かに戻ってしまい、それこそがこの契約が塞ぐ書き込みバイパスです。

`get_backend(policy)` のディスパッチ:

1. `OFF` は即座に `None` を返す: プローブなし、インポートなし、サブプロセスなし。
2. `platform.system()` のプラットフォームレジストリから登録済みファクトリでバックエンドを構築します。Linux は**複合ファクトリ**です：`BwrapBackend.probe()` が成功した場合のみ `BwrapBackend` を返し、失敗すると `LandlockBackend` にフォールバックします（`bwrap` のないスリムコンテナでもサンドボックスなしで実行される代わりにカーネル LSM サンドボックスを得られます）。macOS → `SeatbeltBackend`、Windows → `WinSandboxBackend`。いずれも**遅延インポート**ファクトリとして登録されます(`ImportError` は「利用不可」であってクラッシュではありません)。ファクトリ未登録のプラットフォームにはバックエンドがありません。`register_sandbox_backend(system, factory)` で追加/置換でき、重複登録は `replace=True` がない限り拒否されます。
3. バックエンドが存在しても `probe()` が失敗した場合: `REQUIRED` は `RuntimeError("Required sandbox unavailable on {system}")` を送出し、`AUTO` / `OFF` は `None` を返します。

### ツール統合

`SafeShellTool`(名前 `terminal`)と `TimedPythonREPLTool`(名前 `python_repl`)の `_run`/`_arun` にはサーバー側呼び出し元向けの内部 `sandbox: bool = True` パラメータが残りますが、LLM から見えるスキーマには**ありません**(terminal は `commands`/`run_in_background`/`timeout`、python_repl は `query`)。モデルが偽装した `sandbox=false` はスキーマ検証で破棄されます。PTC の `ExecuteCodeTool`(名前 `execute_code`)には `sandbox` パラメータが一切ありません — executor 専用で、常にサンドボックスを要求します。

- **サンドボックス経路**: `backend.sandbox_shell(cmd_str)` が shell を提供し、`backend.wrap(shell_argv, env, roots)` が包みます。POSIX は `/bin/sh -c`、Windows は AppContainer 有効時に `cmd.exe /c`(Git Bash/MSYS2 は AppContainer 内で初期化できません)、`SHERRY_WIN_SANDBOX_APPCONTAINER=0` なら Git Bash に戻ります(bash が無い場合のみ `COMSPEC`)。python_repl と PTC の子も同じ形です。包まれた argv は list として exec され、シェル kwargs は一切ありません。 `roots` は**セッションのプロジェクトディレクトリ**(`current_project_dir(session_id)` / PTC 子の cwd)で、コマンドの cwd と同じ出所です — だから書き込み面と作業ディレクトリが食い違うことはありません。リポジトリ外に束ねたプロジェクトディレクトリは書き込み可能になり(以前は EROFS)、リポジトリ自体は書き込み不可になります(以前は可能で、それが terminal で外部パスゲートを迂回する経路でした)。`terminal` と `python_repl` はさらにそのセッション自身の `SESSIONS_DIR/<session_id>` ツリー(`with_own_session_root`、必要時に作成)を追加します — ファイルツールが HITL なしで読み書きできる私有の実行状態であり、プロジェクトが別の場所に束ねられていても自分のファイルの作成・変更・削除(`rm`)ができます。追加されるのは**その 1 つ**のセッションディレクトリだけで、共有 sessions ルートと他のセッションは読み取り専用のままです。PTC は自身の cwd を保ちます。
- **裸実行は記録される**: 証拠台帳の行は `sandboxed: false` を運び、サンドボックス迂回の承認カードは `⚠ UNSANDBOXED` とその意味を明示します — 承認する人に見え、後から台帳を監査しても見つかります。
- **フォールバック経路(利用可能なバックエンドなし)**: プラットフォームにバックエンドが無い、ポリシーが `off`、または `probe()` が失敗した場合 — terminal はコマンドを `" && "` で連結し、Git Bash があればそれを使い、無ければ `shell=True` で起動します。python_repl は `[sys.executable, "-c", script]` を list として起動します。メインの HITL 経路では、この降格をツール実行**前**に承認します。ヘッドレスのターンは自動拒否し、サブエージェント/バックグラウンド呼び出しと PTC は拒否します(承認経路なし)。
- **すべての経路で無条件**: `env=scrub_env()` と `cwd=str(ROOT_DIR)`(cwd 固定)。両ツールとも30秒のタイムアウト(`TERMINAL_TIMEOUT`、`PYTHON_REPL_TIMEOUT`)を強制し、期限切れで子を kill します。PTC は自前の `ptc_timeout_seconds` を強制し、子のプロセスグループへ SIGKILL します。
- **エラーの表面化**: `REQUIRED` でバックエンドがない場合、terminal は `RuntimeError` を `ToolException` に包み(`handle_tool_error=True` がそのまま表面化)、python_repl は生の `RuntimeError` をそのまま投げ、PTC は何も spawn する前に `status: "sandbox_unavailable"` エンベロープを返します。
- **降格ログ**: この呼び出しがサンドボックスを望んでいたのにバックエンドがなく、ポリシーが `off` でない場合、`terminal` / `python_repl` はちょうど1件の loguru INFO を記録してからサンドボックスなしで実行します — メインの HITL 経路ではサンドボックス迂回カードがすでに表示・承認されています:

  - `terminal: sandbox unavailable (policy=auto) — running unsandboxed (HITL-gated when an operator is in scope)`
  - `python_repl: sandbox unavailable (policy=auto) — running unsandboxed (HITL-gated when an operator is in scope)`
  - PTC は結果エンベロープで `status: sandbox_unavailable` を返して子を起動しません。`SANDBOX_POLICY=off` のときだけサンドボックスなしで実行します

## 🛠️ 設定と使い方

### `SANDBOX_POLICY`

```bash
# .env またはシェル環境変数
SANDBOX_POLICY=auto      # required | auto | off (大小文字を無視、デフォルト: auto)
```

不正な値は黙ってデフォルトを使う代わりに、最初の使用時点で `ValueError` を送出します。この変数はツール呼び出しごとに再読込されるため、ランタイムで切り替えられます。

### `SANDBOX_STRICT_BOOT`

```bash
# .env またはシェル環境
SANDBOX_STRICT_BOOT=1    # SANDBOX_POLICY=required が満たされないとき起動を拒否
```

既定（未設定）では警告のみで、動作するサンドボックスのない開発ホストでも起動できます。

### `SANDBOX_READ_ALLOWLIST` / `SANDBOX_READ_EXTRA`

```bash
# .env またはシェル環境
SANDBOX_READ_ALLOWLIST=1              # 既定 OFF: 先に scripts/sandbox_smoke.py を実行
SANDBOX_READ_EXTRA="~/.cache:~/.local/share"
```

有効にすると読み取り面は「ファイルシステム全体を読み取り専用マウント」ではなくなります：システムディレクトリ（`/usr`、`/bin`、`/lib`、`/etc` …）、解決された `writable_roots`、そして `SANDBOX_READ_EXTRA`（既定 `~/.cache` — npm/pip/uv のキャッシュ）だけをマウントし、`/home` `/root` `/mnt` `/media` `/srv` `/run` を空ビューで覆います（bwrap）。Seatbelt では読み取りを既定で拒否し、上記パスだけを開け直します。**既定は OFF** です。両バックエンドとも「サンドボックスが実際に動く」マシンで検証されたことがないためです：まず対象ホストで `uv run --no-sync python scripts/sandbox_smoke.py` を実行してください（書き込み面・読み取り面を調べ、`SANDBOX_READ_ALLOWLIST=1` ではホワイトリスト外の読み取りが失敗することも検証します）。有効にしない場合、サンドボックス内でも任意のユーザーファイルを読めます（5 つの資格情報パスと `.env` ファイルのみ遮蔽）。


### `SHERRY_DENY_READ_PATHS`

```bash
# .env またはシェル環境変数(os.pathsep 区切り; ~ は展開)
SHERRY_DENY_READ_PATHS="~/.kube:~/.config/gcloud"
```

両 OS バックエンドのリードシールドリストにパスを追加します([分離 §2](isolation/README.ja.md#2-os-ネイティブサンドボックスバックエンド-l2) の既定リストは常に含まれます)。この変数は `wrap()` 呼び出しごとに読み込まれます。

### Windows サンドボックスの上限(`SHERRY_WIN_SANDBOX_*`)

```bash
# .env またはシェル環境変数(毎回の wrap() 呼び出しで読まれます)
SHERRY_WIN_SANDBOX_MAX_PROCESSES=50     # Job Object ActiveProcessLimit(1..10000 にクランプ)
SHERRY_WIN_SANDBOX_MAX_MEMORY_MB=4096   # Job Object JobMemoryLimit(64..1048576 にクランプ)
SHERRY_WIN_SANDBOX_BLOCK_NETWORK=0      # 1 = internetClient 能力を付与しない(AppContainer はネットワーク不可)
SHERRY_WIN_SANDBOX_APPCONTAINER=1       # 0 = 制限付きトークン + Job Object のみ、サンドボックス shell は Git Bash に戻る
```

解析できない値はデフォルトに戻り、WARNING が 1 行出ます。launcher は適用できなかった各層を Win32 エラーコード付きで stderr に報告し、コマンド自体は実行されます(fail-open)。

### モデルに見えるもの

モデルに見えるのは terminal の `commands` / `run_in_background` / `timeout` と python_repl の `query` だけで、`sandbox` フィールドはありません。スイッチはサーバー側呼び出し元のために `_run`/`_arun` に残り、デフォルトは `True` です。ツールはその後ポリシーを適用します: `required` は内部の `sandbox=False` を拒否し、サブエージェント/バックグラウンドスコープも拒否し、メインセッションだけが下の HITL カードを提供します。

### ユーザーが承認・拒否する方法

サーバー側の呼び出し元がメインセッション(非 YOLO)で `sandbox=False` を要求すると、グラフは `HumanInTheLoop.after_model` インタラプトで停止します。承認ストラテジーは生の呼び出し引数を読むため、モデルがこのフィールドを生成できなくなっても、内部/診断呼び出しのカードは機能します。フロントエンドはこのアクション(ツール名、完全な引数、コマンドまたは query)を描画し、2つの決定を提示します:

- **approve**: `{"decisions": [{"type": "approve"}]}` で再開。呼び出しは即座に実行されます(env は洗浄済み、OS サンドボックスなし)。
- **reject**: `{"decisions": [{"type": "reject", "message": "..."}]}` で再開。ツール結果はエラー `ToolMessage`(`User denied: <msg>. <BLOCKED_MESSAGE>`)になり、何も実行されません。

端末承認カードは、バックエンドが `allowed_decisions` に `session` を含めた場合だけ **セッション内で許可** を表示します（カードに到達したキャッシュ可能な通常コマンド）。選ぶとコマンドの先頭 2 語パターンが session allowlist に書き込まれ、以後の一致コマンドはカードを出しません。**approve** は今回限りのままです。危険コマンド、confirm-all モード、clawhub リモート実行は session キャッシュを提供も書き込みもしません。以前に一致パターンが保存されていても、危険コマンドは session allowlist をスキップします。

### landlock フォールバック(Linux、bwrap なし)

`agent/tools/pub_base/sandbox_landlock.py` は 2 つ目の Linux バックエンドです。bwrap が存在しない環境(スリムコンテナ、distroless イメージ)では、カーネルレベルの landlock LSM(Linux 5.13+)が外部バイナリなしでコマンドを制限します。ルールセットは**許可リスト**方式 — システムツリー(`/usr /bin /sbin /lib /lib64 /etc /opt /dev /proc /sys`)は読み取りのみ、呼び出し側の書き込みルートと `/dev/null` は読み書き可 — であり、`~/.ssh` などの認証情報ディレクトリは**初期状態で除外**されます(bwrap のマスクより強く、「ファイルシステム全体の読み取り」より厳格)。landlock は**呼び出しプロセス自身**しか制限できないため、`wrap()` はランチャー(`python -I -c <loader> -- <本体コマンド>`)を返します。ランチャーはルールを適用し(ctypes、システムコール 444-446、先に `PR_SET_NO_NEW_PRIVS`)、`execvp` で本体コマンドに交代します。初期化に失敗すると終了コード 126 — サンドボックスは**フェイルクローズ**です(`required` 下の bwrap 欠落と同じ)。凍結ビルドと x86_64/aarch64 以外のマシンは非対応を報告します。REFER(ABI v2)と TRUNCATE(ABI v3)はカーネルに応じて扱う権限が増えます。`probe()` は ABI 番号だけでは合格としません。ランチャー経由で実際に `/bin/true` をスモークテストし(bwrap の probe と同様)、結果をキャッシュします。ネスト/コンテナ環境では ABI が見えても `restrict_self` 後に `execve` が拒否されることがあるため(seccomp/`no_new_privs` の相互作用)、その場合は利用不可と報告し `AUTO` では縮退します。そのディストリのレイアウトに存在しない固定読み取りパス(usrmerge 下の `/lib64` など)はスキップされます。存在しないパスの許可リスト規則をスキップしてもアクセス範囲は広がりません。テスト:`tests/agent/tools/pub_base/test_sandbox_landlock.py`(probe/仕様/ファクトリ、スモークテスト成功と exec 拒否の両経路を含む。実際のシステムコール経路には 5.13+ カーネルが必要)。

## 🧪 テスト

| テストスイート | カバー範囲 |
| :------------- | :--------- |
| `tests/agent/tools/test_sandbox_matrix.py` | 14テスト、マトリクスのセルごとの動作につき1つ(セル1-5はツールごとに1回、セル6は4回)。実グラフ上の HITL インタラプトと「INFO ちょうど1件」の降格アサーションを含む |
| `tests/agent/tools/pub_base/test_env_scrub.py` | 洗浄規則、優先順位、保持/拒否の境界 (50テスト) |
| `tests/agent/tools/pub_base/test_sandbox_policy.py` | ポリシーパース、厳格な `ValueError`、即時読み込みの意味論、プラットフォームディスパッチ |
| `tests/agent/tools/pub_base/test_sandbox_bwrap.py` / `test_sandbox_seatbelt.py` | argv / profile 構築(リードシールドのマウントを含む)、プローブキャッシュ (subprocess はすべてモック)、任意実行の実 bwrap リードシールドスモークテスト |
| `tests/agent/tools/pub_base/test_sandbox_win.py` | 設定の解析 / クランプ / フォールバック、launcher ペイロードの忠実性(引用符、`%`、改行)、launcher ソースの各層存在ガード、プローブキャッシュ。さらに Windows 上で stdout/stderr/終了コード/cwd/環境変数の伝播、プロセス上限、メモリ上限、kill-on-close、AppContainer の書き込み/読み取り封じ込め、ネットワークスイッチ、AppContainer 無効時のフォールバックを実測 |
| `tests/agent/tools/pub_base/test_terminal_tool.py` / `test_python_repl_tool.py` | ツール層ガード(危険コマンド / 機密ファイル正規表現)、query-only スキーマ、起動形態、制限ビルトインの障壁、起動前の内省拒否 |
| `tests/agent/tools/pub_base/test_safe_exec.py` | AST ガード: 通常コードは通過、dunder 属性 / リテラル `str.format` の dunder フィールド / `__builtins__` / 構文エラーは行番号付きで拒否 |
| `tests/agent/tools/pub_base/test_path_utils.py` | 外部パスフロー、3つの構造ゲート、シンボリックリンクループ処理、仮想パス描画 |
| `tests/agent/tools/file_tools/test_path_hardening.py` / `test_virtual_paths.py` / `test_search_containment.py` / `test_search_bounds.py` | `O_NOFOLLOW` によるシンボリックリンク / TOCTOU 拒否、仮想パス、検索結果コンテインメント、スキャン境界 |
| `tests/agent/middlewares/test_path_guard.py` | `PathGuard` スクリーニング: トラバーサルコンポーネント、ハード拒否フロア、外部パス素通し、構造化エラー `ToolMessage` |
| `tests/agent/middlewares/humanInTheLoop/test_hitl_characterization.py` | 19テスト、サンドボックス強化前の HITL / terminal レガシー動作を固定 |
| `tests/agent/middlewares/humanInTheLoop/test_hitl_sandbox_bypass.py` | 17テスト、バイパス承認フロー、YOLO 素通し、スコープスタンピング |
| `tests/agent/tools/subagent/test_inherited_tool_policy.py` | `caller_scope="subagent"` スタンピング |
| `tests/agent/tools/ptc/test_rpc_auth.py` / `test_sandbox_integration.py` / `test_runner_hardening.py` / `test_runner.py` | PTC RPC ワンタイムトークン握手、PTC サンドボックスポリシーマッピング、runner のトークンライフサイクルとバックエンドラップ、起動前の内省脱出拒否 |

マトリクステストは `subprocess.Popen` をグローバルにパッチし、ツールモジュールの継ぎ目で `get_backend` をスタブし、環境変数で `SANDBOX_POLICY` を設定して、実際の `read_policy` が各セルで走るようにしています。

## ⚠️ 正直な制限事項

- **bwrap と Seatbelt の構築ロジックはユニットテストのみで、実機の Linux/macOS では検証されていません。** バックエンドのソース docstring が明示しています(「構築ロジックのみ検証、実機検証なし」)。すべてのバックエンドテストは subprocess をモックし、リードシールドにはプローブ失敗時にスキップされる任意の実 bwrap スモークテストが1つあります。ラップ出力は信頼できますが、現時点で実際の分離保証ではありません。
- **Windows では 3 層すべてが稼働しますが、shell に意図的な変更が 1 つあります。** ユーザーごとの AppContainer profile がコマンドにパッケージ SID を与えます。launcher は `writable_roots` に継承可能な書き込み ACE を付与し(既存ファイルにも伝播)、shell と venv のベースインタープリタ(`pyvenv.cfg` の `home`)に読み取り+実行を付与するため、venv の `python.exe` も動作します。制限トークンが全特権を除き、Job Object がプロセス/メモリを制限して launcher の死でツリー全体を殺し、`internetClient` 能力を付けなければネットワークも遮断されます。実際の end-to-end テストが書き込み/読み取り封じ込め、ネットワークスイッチ、stdout/stderr/終了コード、上限、kill-on-close を覆います。**Git Bash(MSYS2)は AppContainer 内で起動できません**。そのためサンドボックス化された shell は `cmd.exe` で、POSIX 専用構文(`;`、`$()`、`export`)は使えません。Git Bash が必要なら `SHERRY_WIN_SANDBOX_APPCONTAINER=0` を設定してください(ファイル/ネットワーク封じ込めを失い、制限トークン + Job Object は維持)。DACL 付与は許可したルートに永続し(パッケージ SID のみ読み書き可)、AppContainer profile は実行時に削除されません。
- **機密ファイル正規表現は緩和であり、障壁ではありません。** リテラルなコマンド形状にしかマッチせず、`dd`、`sed`、`python -c "open(…)"`、`$(< file)`、変数、グロブは層の設計上迂回できます。バックエンドが存在する場合の実際の読み取り障壁は OS リードシールドです。
- **`python_repl` には機密ファイル正規表現がありません。** terminal 専用ゲート([分離 §3](isolation/README.ja.md#3-危険コマンドゲート-terminal-のみ))はそれをカバーしません。代わりにラッパースクリプトがビルトインを制限し(`open`/`__import__`/`getattr`/`type`/`dir`/`vars` なし)、`pub_base/safe_exec.py` が子プロセス起動前に dunder 属性アクセスを拒否します — 別の、より狭い制御であり、読み取りに敏感なホストでは引き続き OS サンドボックスが必要です。
- **PTC は同じバックエンドの注意点を継承します。** `execute_code` は使えるバックエンドがあるとき L2 バックエンドで子を包み、内省による脱出も静的に拒否します(制限付きセットに `getattr`/`type`/`dir`/`vars` なし、dunder 属性禁止)。`SANDBOX_POLICY=auto` で使えるバックエンドがない場合は実行を**拒否**します(PTC には HITL ミドルウェアがなく、降格は承認制ではありませんでした)——明示的な `off` のときだけサンドボックスなしで実行されます。PTC は `allow_network=True` を渡すため、bwrap は loopback RPC ブリッジを到達可能に保つべく `--unshare-all` の隣に `--share-net` を追加します。Windows では同じフラグが `SHERRY_WIN_SANDBOX_BLOCK_NETWORK` を抑制し(AppContainer が `internetClient` を得る)、変数が `1` でも PTC 子プロセスは外向きネットワークを保持します(ファイル/読み取りの閉じ込めは不変)。`terminal` と `python_repl` はプライベートなネットワーク名前空間のままです。PTC のワンタイム RPC トークンとその `/proc/<pid>/environ` 残余は [PTC ページ](../ptc/README.ja.md)に記しています。
- **降格経路はメイン経路では承認制、それ以外はツール層のみ。** `auto` + バックエンドなし: メインセッションの `terminal` / `python_repl` はサンドボックス迂回カードを出してからサンドボックスなしで実行し、ヘッドレスは自動拒否、confirm-all のカードには注記が付き、YOLO は設計どおり迂回します（有効化時に警告）。子エージェント鎖、バックグラウンドグラフ、PTC には HITL ミドルウェアがないため、`auto` でバックエンドがない場合は起動前に拒否します（`_deny_unsandboxed_degrade`）。明示的な `off` だけがサンドボックスなしの経路を残します。同梱 `.env` は `auto` です。ハード保証には `required` を設定し、`SANDBOX_STRICT_BOOT=1` なら起動自体を拒否します。
- **コマンドパスのスクリーンは緩和層であり、障壁ではありません。** ハード拒否パス(資格情報/システムファイル、YOLO 拒否リスト)を**文字どおり名指しした**コマンドだけを拒否し、それ以外はサンドボックスに委ねます。`base64 -d | sh`、断片から組み立てた変数、`cd` + 相対パスはいずれも構造的に回避できます。境界は OS サンドボックスです。
- **環境変数洗浄は名前ベースです。** ブロック対象の部分文字列を1つも含まない名前(かつ拒否リストにない名前)で保存されたシークレットはそのまま通ります。値のスキャンも動的シークレット検出もなく、それは意図的なものです。
- **ネットワークサンドボックス、seccomp、AppArmor プロファイルは主張も設定もしていません。** 分離は [分離 §2](isolation/README.ja.md#2-os-ネイティブサンドボックスバックエンド-l2) に示した bwrap / Seatbelt の構築と、Windows の AppContainer + 制限トークン + Job Object だけです。`SHERRY_WIN_SANDBOX_BLOCK_NETWORK` は AppContainer の `internetClient` 能力を外す(外向きネットワーク不可)もので、ホスト単位のファイアウォールではありません。
