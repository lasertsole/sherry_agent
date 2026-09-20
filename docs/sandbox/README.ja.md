# 🛡️ ツールサンドボックス: terminal と python_repl

[English](README.md) · [中文](README.zh.md) · [한국어](README.ko.md) · **日本語**

> エージェントがモデル発のコマンドをどう制約するか: すべての子プロセス生成時に環境変数を無条件に洗浄し、利用可能なら OS ネイティブのサンドボックスで包み、意図的なバイパスには人間の承認ゲートを置きます。

2つのツールがモデルにあなたのマシン上でのコード実行を許しています: `terminal`(シェルコマンド)と `python_repl`(子プロセス内の Python)。幻覚やプロンプトインジェクションによる1つのコマンドが、環境変数から API キーを読み取ったり、プロジェクト外に書き込んだり、他のプロセスに触れたりできてしまいます。サンドボックス層はこの3つすべてを制限します。

事実の基準(source of truth): `agent/tools/pub_base/env_scrub.py`、`agent/tools/pub_base/sandbox.py`、`agent/tools/pub_base/sandbox_bwrap.py`、`agent/tools/pub_base/sandbox_seatbelt.py`、`agent/tools/pub_base/path_utils.py`、`agent/tools/file_tools/`、`agent/tools/terminal.py`、`agent/tools/python_repl.py`、`agent/middlewares/humanInTheLoop/`、`agent/middlewares/path_guard/`。

## 目次

- [概要と脅威モデル](#-概要と脅威モデル)
- [分離機能と優先順位](isolation/README.ja.md)
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
| **ファイルシステムへの書き込み** | 子がエージェントユーザーの書けるどこへでも記録 | L2 OS サンドボックス (Linux / macOS) |
| **ファイルシステムからの読み取り** | 子が `~/.ssh`、`.env`、各種クレデンシャルストアを読める | L2 リードシールド(機密パスのマスク、Linux / macOS) + terminal の機密ファイル正規表現 |
| **ファイルツールのパス引数** | ツール呼び出しが `read_file` にトラバーサルやハード拒否パスを要求 | §5 外部パスゲート + §6 3つの構造ゲート + §7 `PathGuard`(外部パスは依然 HITL 経由) |
| **プロセス / セッションスコープ** | 子が名前空間を共有し、親より長く生き残り得る | L2 `--unshare-all`、`--die-with-parent` |
| **意図的なバイパス** | モデルが `sandbox=False` を要求 | 人間の承認ゲート (HITL) |

2つの層と1つのゲート — それに加えてファイルツール独自のパス防御スタック:

- **L1. 環境変数洗浄**(`scrub_env`): 無条件、すべての生成時点で実行。人間が `sandbox=False` を承認した場合でも例外なし。
- **L2. OS ネイティブサンドボックス**: Linux は bubblewrap、macOS は Seatbelt — 書き込み封じ込めに加えて機密パスのリードシールド([§2](isolation/README.ja.md#2-os-ネイティブサンドボックスバックエンド-l2)参照)。Windows には OS バックエンドがありません([正直な制限事項](#️-正直な制限事項)参照)。
- **人間の承認ゲート**: `sandbox=False` によるバイパスはメインセッションでのみ可能で、HITL インタラプトを通ります。
- **ファイルツールのパスゲート**([分離 §5–§7](isolation/README.ja.md#5-外部ファイルパスゲートファイルツール)): `resolve_project_path()` の3つの構造ゲートと `O_NOFOLLOW` I/O、仮想パス描画、検索コンテインメント、6段階の外部パス承認フロー、そして `PathGuard` ミドルウェアのスクリーニング。

## ⚙️ 実装とアーキテクチャ

### ポリシー: `SandboxPolicy`

`SANDBOX_POLICY` 環境変数からパースされる3つの状態:

| 値 | 意味 |
| :- | :--- |
| `required` | バックエンド利用不可 ⇒ コマンドを拒否、サンドボックスなしでは決して実行しない |
| `auto` (デフォルト) | バックエンド利用不可 ⇒ 警告1件とともにサンドボックスなし実行へ降格 |
| `off` | サンドボックス完全無効 |

`parse_policy` は空白を除去し大小文字を無視してマッチし、未知の値には `ValueError` を送出します: 誤入力された安全設定は大きな音で失敗すべきで、黙ってデフォルトにフォールバックしてはいけません。`read_policy()` は**毎回** `os.getenv` を呼びます(インポート時キャッシュなし)。そのためランタイムの変更が即座に反映されます。

### バックエンド契約とディスパッチ

`SandboxBackend` はすべてのバックエンドが実装する ABC です:

- `probe() -> bool`: 決して例外を送出してはいけません。バックエンドが自身のプローブ例外を捕捉して `False` を返します。
- `wrap(cmd, env) -> (argv, env)`: 包まれた argv と env を返し、list 形式で直接 exec されます(シェルなし)。

`get_backend(policy)` のディスパッチ:

1. `OFF` は即座に `None` を返す: プローブなし、インポートなし、サブプロセスなし。
2. Linux は `BwrapBackend` を、macOS は `SeatbeltBackend` を遅延インポートします(`ImportError` は「利用不可」であってクラッシュではありません)。それ以外のプラットフォーム、Windows を含む、にはバックエンドがありません。
3. バックエンドが存在しても `probe()` が失敗した場合: `REQUIRED` は `RuntimeError("Required sandbox unavailable on {system}")` を送出し、`AUTO` / `OFF` は `None` を返します。

### ツール統合

`SafeShellTool`(名前 `terminal`)と `TimedPythonREPLTool`(名前 `python_repl`)はどちらも LLM から見えるツール呼び出しスキーマに `sandbox: bool = True` パラメータを露出しており、モデルが呼び出しごとに選択します。

- **サンドボックス経路**: terminal は `backend.wrap(["/bin/sh", "-c", cmd_str], env)`(POSIX `shell=True` と意味的に同一)、python_repl は `backend.wrap([sys.executable, "-c", script], env)` を使います。包まれた argv は list として exec され、シェル kwargs は一切ありません。
- **フォールバック経路(Windows / バックエンドなし)**: terminal はコマンドを `" && "` で連結して `shell=True` で起動し、python_repl は `[sys.executable, "-c", script]` を list として起動します。Windows には OS サンドボックスバックエンドが**ありません**。
- **すべての経路で無条件**: `env=scrub_env()` と `cwd=str(ROOT_DIR)`(cwd 固定)。両ツールとも30秒のタイムアウト(`TERMINAL_TIMEOUT`、`PYTHON_REPL_TIMEOUT`)を強制し、期限切れで子を kill します。
- **エラーの表面化**: `REQUIRED` でバックエンドがない場合、terminal は `RuntimeError` を `ToolException` に包み(`handle_tool_error=True` がそのまま表面化)、python_repl は生の `RuntimeError` をそのまま投げます。
- **降格警告**: この呼び出しがサンドボックスを望んでいたのにバックエンドがなく、ポリシーが `off` でない場合、ツール層はちょうど1件の loguru 警告を記録してからサンドボックスなしで実行します:

  - `terminal: sandbox requested but no backend available (policy=auto) — degrading to unsandboxed shell execution`
  - `python_repl: sandbox requested but no backend available (policy=auto) — degrading to unsandboxed execution`

## 🛠️ 設定と使い方

### `SANDBOX_POLICY`

```bash
# .env またはシェル環境変数
SANDBOX_POLICY=auto      # required | auto | off (大小文字を無視、デフォルト: auto)
```

不正な値は黙ってデフォルトを使う代わりに、最初の使用時点で `ValueError` を送出します。この変数はツール呼び出しごとに再読込されるため、ランタイムで切り替えられます。

### `SHERRY_DENY_READ_PATHS`

```bash
# .env またはシェル環境変数(os.pathsep 区切り; ~ は展開)
SHERRY_DENY_READ_PATHS="~/.kube:~/.config/gcloud"
```

両 OS バックエンドのリードシールドリストにパスを追加します([分離 §2](isolation/README.ja.md#2-os-ネイティブサンドボックスバックエンド-l2) の既定リストは常に含まれます)。この変数は `wrap()` 呼び出しごとに読み込まれます。

### モデルに見えるもの

両ツールとも呼び出しごとの `sandbox` ブーリアンを受け付け、デフォルトは `True` です。ツールの説明はモデルにこう伝えます: `false` はメインセッションで人間の承認を経て洗浄済み環境で実行されること、サブエージェントとバックグラウンドエージェントの要求は拒否されること。

### ユーザーが承認・拒否する方法

メインセッション(非 YOLO)でモデルが `sandbox=False` を要求すると、グラフは `HumanInTheLoop.after_model` インタラプトで停止します。フロントエンドはこのアクション(ツール名、完全な引数、コマンドまたは query)を描画し、2つの決定を提示します:

- **approve**: `{"decisions": [{"type": "approve"}]}` で再開。呼び出しは即座に実行されます(env は洗浄済み、OS サンドボックスなし)。
- **reject**: `{"decisions": [{"type": "reject", "message": "..."}]}` で再開。ツール結果はエラー `ToolMessage`(`User denied: <msg>. <BLOCKED_MESSAGE>`)になり、何も実行されません。

## 🧪 テスト

| テストスイート | カバー範囲 |
| :------------- | :--------- |
| `tests/agent/tools/test_sandbox_matrix.py` | 14テスト、マトリクスのセルごとの動作につき1つ(セル1-5はツールごとに1回、セル6は4回)。実グラフ上の HITL インタラプトと「警告ちょうど1件」の降格アサーションを含む |
| `tests/agent/tools/pub_base/test_env_scrub.py` | 洗浄規則、優先順位、保持/拒否の境界 (29テスト) |
| `tests/agent/tools/pub_base/test_sandbox_policy.py` | ポリシーパース、厳格な `ValueError`、即時読み込みの意味論、プラットフォームディスパッチ |
| `tests/agent/tools/pub_base/test_sandbox_bwrap.py` / `test_sandbox_seatbelt.py` | argv / profile 構築(リードシールドのマウントを含む)、プローブキャッシュ (subprocess はすべてモック)、任意実行の実 bwrap リードシールドスモークテスト |
| `tests/agent/tools/pub_base/test_terminal_tool.py` / `test_python_repl_tool.py` | ツール層ガード(危険コマンド / 機密ファイル正規表現)、スキーマ、起動形態、制限ビルトインの障壁 |
| `tests/agent/tools/pub_base/test_path_utils.py` | 外部パスフロー、3つの構造ゲート、シンボリックリンクループ処理、仮想パス描画 |
| `tests/agent/tools/file_tools/test_path_hardening.py` / `test_virtual_paths.py` / `test_search_containment.py` / `test_search_bounds.py` | `O_NOFOLLOW` によるシンボリックリンク / TOCTOU 拒否、仮想パス、検索結果コンテインメント、スキャン境界 |
| `tests/agent/middlewares/test_path_guard.py` | `PathGuard` スクリーニング: トラバーサルコンポーネント、ハード拒否フロア、外部パス素通し、構造化エラー `ToolMessage` |
| `tests/agent/middlewares/humanInTheLoop/test_hitl_characterization.py` | 19テスト、サンドボックス強化前の HITL / terminal レガシー動作を固定 |
| `tests/agent/middlewares/humanInTheLoop/test_hitl_sandbox_bypass.py` | 17テスト、バイパス承認フロー、YOLO 素通し、スコープスタンピング |
| `tests/agent/tools/subagent/test_inherited_tool_policy.py` | `caller_scope="subagent"` スタンピング |

マトリクステストは `subprocess.Popen` をグローバルにパッチし、ツールモジュールの継ぎ目で `get_backend` をスタブし、環境変数で `SANDBOX_POLICY` を設定して、実際の `read_policy` が各セルで走るようにしています。

## ⚠️ 正直な制限事項

- **bwrap と Seatbelt の構築ロジックはユニットテストのみで、実機の Linux/macOS では検証されていません。** バックエンドのソース docstring が明示しています(「構築ロジックのみ検証、実機検証なし」)。すべてのバックエンドテストは subprocess をモックし、リードシールドにはプローブ失敗時にスキップされる任意の実 bwrap スモークテストが1つあります。ラップ出力は信頼できますが、現時点で実際の分離保証ではありません。
- **Windows には OS サンドボックスバックエンドがありません。** そこの防御は環境変数洗浄 + cwd 固定 + 危険コマンド正規表現 + 機密ファイル正規表現 + HITL ゲートです。プロジェクトルート外へのファイル書き込みを防ぐ仕組みはなく、**読み取り保護も利用できません**: OS バックエンドが無ければリードシールドも無く、アプリ層の正規表現が唯一の読み取りゲートです。
- **機密ファイル正規表現は緩和であり、障壁ではありません。** リテラルなコマンド形状にしかマッチせず、`dd`、`sed`、`python -c "open(…)"`、`$(< file)`、変数、グロブは層の設計上迂回できます。バックエンドが存在する場合の実際の読み取り障壁は OS リードシールドです。
- **`python_repl` には機密ファイル正規表現がありません。** terminal 専用ゲート([分離 §3](isolation/README.ja.md#3-危険コマンドゲート-terminal-のみ))はそれをカバーしません。代わりにラッパースクリプトがビルトインを制限します(安全なサブセットは `open` / `__import__` を含みません) — 別の、より狭い制御です。
- **降格経路は設計どおりサンドボックスなしで実行されます。** `auto` + バックエンドなし = 警告1件を記録してから普段どおりサンドボックスなしで実行。これは意図された「可用性優先」の選択で、逆が必要なら `SANDBOX_POLICY=required` を選んでください。
- **環境変数洗浄は名前ベースです。** ブロック対象の部分文字列を1つも含まない名前(かつ拒否リストにない名前)で保存されたシークレットはそのまま通ります。値のスキャンも動的シークレット検出もなく、それは意図的なものです。
- **ネットワークサンドボックス、seccomp、AppArmor プロファイルは主張も設定もしていません。** 分離は [分離 §2](isolation/README.ja.md#2-os-ネイティブサンドボックスバックエンド-l2) に示した bwrap / Seatbelt の構築そのものだけです。
