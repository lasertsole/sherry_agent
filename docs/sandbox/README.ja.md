# 🛡️ ツールサンドボックス: terminal と python_repl

[English](README.md) · [中文](README.zh.md) · [한국어](README.ko.md) · **日本語**

> エージェントがモデル発のコマンドをどう制約するか: すべての子プロセス生成時に環境変数を無条件に洗浄し、利用可能なら OS ネイティブのサンドボックスで包み、意図的なバイパスには人間の承認ゲートを置きます。

2つのツールがモデルにあなたのマシン上でのコード実行を許しています: `terminal`(シェルコマンド)と `python_repl`(子プロセス内の Python)。幻覚やプロンプトインジェクションによる1つのコマンドが、環境変数から API キーを読み取ったり、プロジェクト外に書き込んだり、他のプロセスに触れたりできてしまいます。サンドボックス層はこの3つすべてを制限します。

事実の基準(source of truth): `agent/tools/pub_base/env_scrub.py`、`agent/tools/pub_base/sandbox.py`、`agent/tools/pub_base/sandbox_bwrap.py`、`agent/tools/pub_base/sandbox_seatbelt.py`、`agent/tools/terminal.py`、`agent/tools/python_repl.py`、`agent/middlewares/humanInTheLoop/`。

## 🎯 概要と脅威モデル

| 露出面 | サンドボックスがない場合 | 防御 |
| :----- | :----------------------- | :--- |
| **環境変数内のシークレット** | 子プロセスが `*_API_KEY` を含む全変数を継承 | L1 環境変数洗浄 |
| **ファイルシステムへの書き込み** | 子がエージェントユーザーの書けるどこへでも記録 | L2 OS サンドボックス (Linux / macOS) |
| **プロセス / セッションスコープ** | 子が名前空間を共有し、親より長く生き残り得る | L2 `--unshare-all`、`--die-with-parent` |
| **意図的なバイパス** | モデルが `sandbox=False` を要求 | 人間の承認ゲート (HITL) |

2つの層と1つのゲート:

- **L1. 環境変数洗浄**(`scrub_env`): 無条件、すべての生成時点で実行。人間が `sandbox=False` を承認した場合でも例外なし。
- **L2. OS ネイティブサンドボックス**: Linux は bubblewrap、macOS は Seatbelt。Windows には OS バックエンドがありません([正直な制限事項](#️-正直な制限事項)参照)。
- **人間の承認ゲート**: `sandbox=False` によるバイパスはメインセッションでのみ可能で、HITL インタラプトを通ります。

## 🧱 分離機能

### 1. 環境変数洗浄(`scrub_env`)、L1、無条件

`scrub_env(base_env=None)` はすべての子プロセスに渡す安全な環境ディクショナリを作ります。純粋関数で(`os` / `re` のみ、IO なし、ログなし)、入力を決して変更せず、値は検査せず変数**名**だけを見ます。両ツールの同期・非同期の生成地点すべてで実行され、承認済みの `sandbox=False` 呼び出しでも実行されます。

| ルール区分 | マッチ規則 | 結果 | 例 |
| :--------- | :--------- | :--- | :--- |
| **完全一致名で保持** | 大小文字を無視した完全一致 | 保持、すべての拒否規則より優先 | `PATH`、`HOME`、`USER`、`USERNAME`、`LANG`、`TERM`、`TMPDIR`、`TMP`、`TEMP`、`SHELL`、`LOGNAME`、`PYTHONPATH`、`PYTHONUTF8`、`VIRTUAL_ENV`、`COMPUTERNAME`、`SYSTEMROOT`、`SYSTEMDRIVE`、`WINDIR`、`COMSPEC`、`PATHEXT`、`OS`、`PROCESSOR_ARCHITECTURE`、`NUMBER_OF_PROCESSORS`、`APPDATA`、`LOCALAPPDATA`、`USERPROFILE`、`HOMEDRIVE`、`HOMEPATH` |
| **接頭辞で保持** | 名前が `LC_`、`XDG_`、`CONDA` で始まる | 保持、すべての拒否規則より優先 | `LC_ALL`、`XDG_CONFIG_HOME`、`CONDA_TOKEN` |
| **強制拒否(プロジェクトのシークレット)** | 大小文字を無視した完全一致 | 常に除去 | `MAIN_LLM_API_KEY`、`REASONER_LLM_API_KEY`、`AUXILIARY_LLM_API_KEY`、`TAVILY_API_KEY`、`LANGSMITH_API_KEY`、`ITTT_API_KEY`、`VTTT_API_KEY`、`TTI_API_KEY`、`RERANKER_API_KEY`、`EMBEDDING_API_KEY`、`STT_API_KEY` |
| **部分文字列ブロック** | 名前が `KEY`、`TOKEN`、`SECRET`、`PASSWORD`、`CREDENTIAL`、`PASSWD`、`AUTH`、`DSN`、`WEBHOOK`、`BEARER`、`APIKEY` のいずれかを含む(大小文字を無視) | 除去 | `MY_CUSTOM_TOKEN`、`AWS_SECRET_ACCESS_KEY` |
| **そのまま通過** | どの規則にも該当しない | 変更せず保持 | `EDITOR`、`GIT_AUTHOR_NAME` |

- **優先順位**: 保持(完全 / 接頭辞) > 強制拒否 > 部分文字列ブロック。`CONDA_TOKEN` は `TOKEN` を含みますが接頭辞保持で生き残り、`PATH` を*含むだけ*の名前(例: `KEY_PATH_DELIM`)は保持名ではないため `KEY` の部分文字列規則で除去されます。
- これは**ホワイトリストではありません**: どの規則にも当てはまらない変数はそのまま通ります(ホワイトリスト専用モードは `PATH` を失わせて子プロセスを壊します)。
- 除去された変数名はログに書かれないため、シークレット名がログに漏れることはありません。

### 2. OS ネイティブサンドボックスバックエンド (L2)

**Linux: bubblewrap(`bwrap`)**。コマンドは順序が耐荷重構造である list-exec 形式の argv で包まれます:

```text
bwrap
  --ro-bind / /                              # ルートファイルシステム全体: 読み取り専用
  --bind <プロジェクトルート> <プロジェクトルート>   # 唯一の書き込み可能な場所:
  --bind <一時ディレクトリ> <一時ディレクトリ>       # プロジェクトルート + 一時ディレクトリ(同一なら重複除去)
  --tmpfs /tmp  --dev /dev  --proc /proc
  --unshare-all                              # 全名前空間を非共有化
  --die-with-parent  --new-session
  --clearenv                                 # 環境を空にする、すべての --setenv より前
  --setenv <K> <V> ...                       # 洗浄済みの変数だけ再注入
  -- /bin/sh -c "<コマンド>"                  # 包まれたコマンド
```

`--clearenv` がすべての `--setenv` より先に来ることと組み合わせて、洗浄済みディクショナリが本当の環境変数ホワイトリストになります。ルートファイルシステムは読み取り専用で、書き込みはプロジェクトルートと一時ディレクトリにしか落ちません。

**macOS: Seatbelt(`sandbox-exec`)**。コマンドは `sandbox-exec -p <profile> -- <cmd...>` として実行され、profile は次のとおりです:

```text
(version 1)
(allow default)
(deny file-write*)
(allow file-write* (subpath "<プロジェクトルート>"))
(allow file-write* (subpath "<一時ディレクトリ>"))
(allow file-write* (literal "/dev/null"))
(allow file-write* (literal "/dev/tty"))
```

順序こそが仕様です: `(allow default)` の下の `(deny file-write*)` は「ファイル書き込み以外はすべて許可」を意味し、その後の明示的 allow が2つの書き込み可能パスと `/dev/null`、`/dev/tty` のリテラルを再び開きます。パスは `json.dumps` で埋め込まれ、パス中の引用符やバックスラッシュが sbpl 注入コードとして脱出することはできません。

**プローブ(可用性確認)**。両バックエンドともクラスレベルキャッシュ付きの `probe() -> bool` を実装します(プロセスにつき1回プローブ、失敗結果もキャッシュ):

- `BwrapBackend.probe()`: `bwrap --ro-bind / / --proc /proc --dev /dev true` を3秒のタイムアウトでスモーク実行。バイナリの存在だけでは不十分です。Ubuntu 24.04+ の AppArmor 非特権 user namespace 制限は uid-map 段階ですべての bwrap を殺せます。だからこそ実際のスモーク実行だけが誠実な確認です。
- `SeatbeltBackend.probe()`: `shutil.which("sandbox-exec")` のみ。sbpl には終了コードベースのスモークプローブがありません。

### 3. 危険コマンドゲート (terminal のみ)

`DANGEROUS_COMMAND_REGEX` は6つの選択肢パターンからなるブラックリスト正規表現で、`" && "` で連結した完全なコマンド文字列に対して `re.IGNORECASE` でマッチし、どの生成よりも先に実行されます:

| # | パターンの意図 | 引っかかる例 |
| :- | :------------- | :----------- |
| 1 | `/` または `~` を狙う再帰/強制 `rm` | `rm -rf /`、`rm -fr ~` |
| 2 | 再帰フラグ付きのすべての `rm` | `rm -r build/` |
| 3 | `mkfs` | ファイルシステムのフォーマット |
| 4 | `shutdown` | システムシャットダウン |
| 5 | `reboot` | システム再起動 |
| 6 | `|`、`&&`、`;` の後に `rm` / `shutdown` / `reboot` / `mkfs` | `echo ok && rm -rf /` のような連鎖バリアント |

**連結後**の文字列をマッチすることに意味があります: 旧来の要素単位の完全一致ブラックリストは、各要素を単独で見れば無害に見える `["echo ok", "rm -rf /"]` を見逃していました。マッチすると `ToolException("Blocked: unsafe command.")` を送出し、`handle_tool_error=True` を経由してエラーのツール結果として表面化します。このゲートは `sandbox` の値にかかわらず常に動きます。`python_repl` には対応する正規表現がなく、代わりにラッパースクリプトがビルトインを制限します。

### 4. 人間が承認するバイパス経路

`sandbox=False` の呼び出しは意図的なバイパス要求です。`HumanInTheLoop` ミドルウェアが動作していて YOLO でない**メインセッション**のグラフでは、`after_model` がその呼び出しを LangGraph の `interrupt()` の上に停めます:

- インタラプトのペイロードは完全なツール呼び出し(ツール名、引数、コマンドまたは query)を示し、`allowed_decisions: ["approve", "reject"]` を含みます。
- **承認**(`{"decisions": [{"type": "approve"}]}`): 元の引数のままで実行されます。環境は依然として洗浄され、cwd はプロジェクトルートに固定され、危険コマンド正規表現も依然として適用されます。承認されたバイパスはスマート承認と危険コマンドの再確認を省きます。人間がこの呼び出し全体を承認したからで、ハードラインのブラックリストはその前にすでに走っています。
- **拒否**(または決定なし): 結果が内容 `User denied: <msg>. <BLOCKED_MESSAGE>` のエラー `ToolMessage` に置き換えられます。コマンドは決して実行されず、2つ目のインタラプトも発生しません。`GraphInterrupt` は飲み込まれずに再送出されます。
- **YOLO モード**(`is_yolo_mode`: `config.yolo_mode`、または `ApprovalMode.OFF`、または環境変数 `SHERRY_YOLO_MODE` が `1` / `true` / `yes`): インタラプトを省き、直接実行します(環境洗浄は依然適用)。
- **バックグラウンド / サブエージェントスコープ**: heartbeat と cron のツールには `caller_scope="background"` がスタンプされ、サブエージェントパイプラインは `caller_scope="subagent"` をスタンプします。それらのグラフには HITL ミドルウェアがないため、ツール層自体が `sandbox=False` を `ToolException` で強制拒否します。そこにインタラプトは存在せず、必要でもありません。

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
- **フォールバック経路(Windows / バックエンドなし)**: 元の構築方法をバイト単位でそのまま保ち、`env=` だけを追加します。terminal はコマンドを `" && "` で連結して `shell=True` で起動し、python_repl は `[sys.executable, "-c", script]` を list として起動します。Windows には OS サンドボックスバックエンドが**ありません**。
- **すべての経路で無条件**: `env=scrub_env()` と `cwd=str(ROOT_DIR)`(cwd 固定)。両ツールとも30秒のタイムアウト(`TERMINAL_TIMEOUT`、`PYTHON_REPL_TIMEOUT`)を強制し、期限切れで子を kill します。
- **エラーの表面化**: `REQUIRED` でバックエンドがない場合、terminal は `RuntimeError` を `ToolException` に包み(`handle_tool_error=True` がそのまま表面化)、python_repl は生の `RuntimeError` をそのまま投げます。
- **降格警告**: この呼び出しがサンドボックスを望んでいたのにバックエンドがなく、ポリシーが `off` でない場合、ツール層はちょうど1件の loguru 警告を記録してからサンドボックスなしで実行します:

  - `terminal: sandbox requested but no backend available (policy=auto) — degrading to unsandboxed shell execution`
  - `python_repl: sandbox requested but no backend available (policy=auto) — degrading to unsandboxed execution`

## 📊 優先順位マトリクス

`agent/tools/pub_base/sandbox.py` 由来の権威ある表で、`tests/integration/test_sandbox_matrix.py` がセルごとにテストします:

| # | ポリシー | `sandbox` フラグ | バックエンド可? | 呼び出し元スコープ | 結果 |
| :- | :------- | :--------------- | :-------------- | :----------------- | :--- |
| 1 | `required` | `True` | はい | すべて | バックエンドラップ内で実行 (list-exec、洗浄済み env) |
| 2 | `required` | `True` | いいえ | すべて | `RuntimeError` / ツールエラー、何も生成されない |
| 3 | `required` | `False` | (照会しない) | すべて | ツール層の `ToolException`、決して `GraphInterrupt` ではない、生成なし |
| 4 | `auto` | `False` | (照会しない) | メイン、非 YOLO | HITL インタラプト: 承認 → 実行(依然洗浄済み)、拒否 → エラー `ToolMessage` |
| 5 | `auto` | `True` | いいえ | すべて | 降格: サンドボックスなしで直接実行、ちょうど1件の警告、env は依然洗浄済み |
| 6 | `off` | `True` / `False` | プローブしない | メイン | サンドボックスなし、承認なし、警告なし、そのまま実行 |

補足:

- `auto` + `True` + バックエンド利用可能はセル1と同じです: バックエンドラップ内で実行。
- 呼び出し元スコープのガードはポリシー処理の前に走るツール層の検査です: メイン以外のスコープ(`subagent`、`background`)からの `sandbox=False` 要求は、すべてのポリシーで `ToolException` として強制拒否されます。それらのグラフには承認インタラプトが存在しないためです。したがってセル4のインタラプトはメインスコープの呼び出しにだけ発生します。

## 🛠️ 設定と使い方

### `SANDBOX_POLICY`

```bash
# .env またはシェル環境変数
SANDBOX_POLICY=auto      # required | auto | off (大小文字を無視、デフォルト: auto)
```

不正な値は黙ってデフォルトを使う代わりに、最初の使用時点で `ValueError` を送出します。この変数はツール呼び出しごとに再読込されるため、ランタイムで切り替えられます。

### モデルに見えるもの

両ツールとも呼び出しごとの `sandbox` ブーリアンを受け付け、デフォルトは `True` です。ツールの説明はモデルにこう伝えます: `false` はメインセッションで人間の承認を経て洗浄済み環境で実行されること、サブエージェントとバックグラウンドエージェントの要求は拒否されること。

### ユーザーが承認・拒否する方法

メインセッション(非 YOLO)でモデルが `sandbox=False` を要求すると、グラフは `HumanInTheLoop.after_model` インタラプトで停止します。フロントエンドはこのアクション(ツール名、完全な引数、コマンドまたは query)を描画し、2つの決定を提示します:

- **approve**: `{"decisions": [{"type": "approve"}]}` で再開。呼び出しは即座に実行されます(env は洗浄済み、OS サンドボックスなし)。
- **reject**: `{"decisions": [{"type": "reject", "message": "..."}]}` で再開。ツール結果はエラー `ToolMessage`(`User denied: <msg>. <BLOCKED_MESSAGE>`)になり、何も実行されません。

## 🧪 テスト

| テストスイート | カバー範囲 |
| :------------- | :--------- |
| `tests/integration/test_sandbox_matrix.py` | 14テスト、マトリクスのセルごとの動作につき1つ(セル1-5はツールごとに1回、セル6は4回)。実グラフ上の HITL インタラプトと「警告ちょうど1件」の降格アサーションを含む |
| `tests/module/test_env_scrub.py` | 洗浄規則、優先順位、保持/拒否の境界 (29テスト) |
| `tests/module/test_sandbox_policy.py` | ポリシーパース、厳格な `ValueError`、即時読み込みの意味論、プラットフォームディスパッチ |
| `tests/module/test_sandbox_bwrap.py` / `test_sandbox_seatbelt.py` | argv / profile 構築、プローブキャッシュ (subprocess はすべてモック) |
| `tests/module/test_terminal_tool.py` / `test_python_repl_tool.py` | ツール層ガード、スキーマ、起動形態 |
| `tests/module/test_hitl_characterization.py` | 19テスト、サンドボックス強化前の HITL / terminal レガシー動作を固定 |
| `tests/module/test_hitl_sandbox_bypass.py` | 17テスト、バイパス承認フロー、YOLO 素通し、スコープスタンピング |
| `tests/unit/subagent/test_inherited_tool_policy.py` | `caller_scope="subagent"` スタンピング |

マトリクステストは `subprocess.Popen` をグローバルにパッチし、ツールモジュールの継ぎ目で `get_backend` をスタブし、環境変数で `SANDBOX_POLICY` を設定して、実際の `read_policy` が各セルで走るようにしています。

## ⚠️ 正直な制限事項

- **bwrap と Seatbelt の構築ロジックはユニットテストのみで、実機の Linux/macOS では検証されていません。** バックエンドのソース docstring が明示しています(「構築ロジックのみ検証、実機検証なし」)。すべてのバックエンドテストは subprocess をモックします。ラップ出力は信頼できますが、現時点で実際の分離保証ではありません。
- **Windows には OS サンドボックスバックエンドがありません。** そこの防御は環境変数洗浄 + cwd 固定 + 危険コマンド正規表現 + HITL ゲートです。プロジェクトルート外へのファイル書き込みを防ぐ仕組みはありません。
- **降格経路は設計どおりサンドボックスなしで実行されます。** `auto` + バックエンドなし = 警告1件を記録してから普段どおりサンドボックスなしで実行。これは意図された「可用性優先」の選択で、逆が必要なら `SANDBOX_POLICY=required` を選んでください。
- **環境変数洗浄は名前ベースです。** ブロック対象の部分文字列を1つも含まない名前(かつ拒否リストにない名前)で保存されたシークレットはそのまま通ります。値のスキャンも動的シークレット検出もなく、それは意図的なものです。
- **ネットワークサンドボックス、seccomp、AppArmor プロファイルは主張も設定もしていません。** 分離は上に示した bwrap / Seatbelt の構築そのものだけです。
