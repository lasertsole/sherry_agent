# 🛡️ ツールサンドボックス: terminal と python_repl

[English](README.md) · [中文](README.zh.md) · [한국어](README.ko.md) · **日本語**

> エージェントがモデル発のコマンドをどう制約するか: すべての子プロセス生成時に環境変数を無条件に洗浄し、利用可能なら OS ネイティブのサンドボックスで包み、意図的なバイパスには人間の承認ゲートを置きます。

2つのツールがモデルにあなたのマシン上でのコード実行を許しています: `terminal`(シェルコマンド)と `python_repl`(子プロセス内の Python)。幻覚やプロンプトインジェクションによる1つのコマンドが、環境変数から API キーを読み取ったり、プロジェクト外に書き込んだり、他のプロセスに触れたりできてしまいます。サンドボックス層はこの3つすべてを制限します。

事実の基準(source of truth): `agent/tools/pub_base/env_scrub.py`、`agent/tools/pub_base/sandbox.py`、`agent/tools/pub_base/sandbox_bwrap.py`、`agent/tools/pub_base/sandbox_seatbelt.py`、`agent/tools/pub_base/path_utils.py`、`agent/tools/file_tools/`、`agent/tools/terminal.py`、`agent/tools/python_repl.py`、`agent/middlewares/humanInTheLoop/`、`agent/middlewares/path_guard/`。

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
- **L2. OS ネイティブサンドボックス**: Linux は bubblewrap、macOS は Seatbelt — 書き込み封じ込めに加えて機密パスのリードシールド([§2](#2-os-ネイティブサンドボックスバックエンド-l2)参照)。Windows には OS バックエンドがありません([正直な制限事項](#️-正直な制限事項)参照)。
- **人間の承認ゲート**: `sandbox=False` によるバイパスはメインセッションでのみ可能で、HITL インタラプトを通ります。
- **ファイルツールのパスゲート**(§5–§7): `resolve_project_path()` の3つの構造ゲートと `O_NOFOLLOW` I/O、仮想パス描画、検索コンテインメント、6段階の外部パス承認フロー、そして `PathGuard` ミドルウェアのスクリーニング。

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
  --ro-bind /var/empty <機密ディレクトリ>      # リードシールド: 機密ディレクトリを空ディレクトリでマスク
  --ro-bind /dev/null <機密ファイル>           # リードシールド: 機密ファイルをマスク
                                             # (/var/empty が無い場合は --tmpfs <パス>)
  --tmpfs /tmp  --dev /dev  --proc /proc
  --unshare-all                              # 全名前空間を非共有化
  --die-with-parent  --new-session
  --clearenv                                 # 環境を空にする、すべての --setenv より前
  --setenv <K> <V> ...                       # 洗浄済みの変数だけ再注入
  -- /bin/sh -c "<コマンド>"                  # 包まれたコマンド
```

`--clearenv` がすべての `--setenv` より先に来ることと組み合わせて、洗浄済みディクショナリが本当の環境変数ホワイトリストになります。ルートファイルシステムは読み取り専用で、書き込みはプロジェクトルートと一時ディレクトリにしか落ちません。

**リードシールド。** `--ro-bind / /` は読み取りを「どこでも可能」にするだけで、無害にはしません。マスクがなければモデルは `cat ~/.ssh/id_rsa` を実行できます。そこで両バックエンドは既定の機密パスリスト `DEFAULT_DENY_READ_PATHS` — `~/.ssh`、`~/.aws`、`~/.gnupg`、`~/.config/gh`、`~/.docker` — をマスクし、呼び出しごとに `_sensitive_read_paths()` が解決し、環境変数 `SHERRY_DENY_READ_PATHS`(`os.pathsep` 区切り、`~` 展開、空項目はスキップ、順序は保持、重複は除去)で拡張できます。bwrap のシールドは存在する各ディレクトリの上に空ディレクトリをマウントし(機密ファイルには `--ro-bind /dev/null`)、存在しないパスはスキップします(読むものが無く、bwrap は読み取り専用ルートバインドの下にマウントポイントを作れません)。`/var/empty` が無いホストではディレクトリは `--tmpfs <パス>` にフォールバックします。シールドは書き込み可能バインドの**後**に置かれ、書き込み可能なプロジェクトルートがマスク済みパスを再露出させることはありません。

**macOS: Seatbelt(`sandbox-exec`)**。コマンドは `sandbox-exec -p <profile> -- <cmd...>` として実行され、profile は次のとおりです:

```text
(version 1)
(allow default)
(deny file-write*)
(deny file-read* (subpath "<機密パス>"))            # 機密パスごとに1行、~ 展開
(deny file-read* (regex #"(^|/)\.env$"))           # 任意の深さの .env / .env.*
(deny file-read* (regex #"(^|/)\.env\."))
(allow file-write* (subpath "<プロジェクトルート>"))
(allow file-write* (subpath "<一時ディレクトリ>"))
(allow file-write* (literal "/dev/null"))
(allow file-write* (literal "/dev/tty"))
```

順序こそが仕様です: `(allow default)` の下の `(deny file-write*)` は「ファイル書き込み以外はすべて許可」を意味し、その後の明示的 allow が2つの書き込み可能パスと `/dev/null`、`/dev/tty` のリテラルを再び開きます。リードシールドの `deny file-read*` ルールは `(deny file-write*)` の直後に置かれます: 機密パスごとに1つの `subpath` ルール(既定リストと `SHERRY_DENY_READ_PATHS` 拡張は bwrap と共通)と、任意の場所の `.env` / `.env.*` を覆う2つの正規表現ルールです。存在しないパスも拒否されます — 存在しないパスへの拒否は無害です。パスは `json.dumps` で埋め込まれ、パス中の引用符やバックスラッシュが sbpl 注入コードとして脱出することはできません。

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

**機密ファイルゲート (`_SENSITIVE_FILE_PATTERNS`)。** `_run` と `_arun` の両方で、`_check_sensitive_file_access(cmd_str)` は `_check_dangerous` の**後**、**どの生成よりも前**に実行されます: 6つのコンパイル済みパターンのいずれかが連結後のコマンド文字列にマッチすると `ToolException("Blocked: sensitive file access. …")`(`_SENSITIVE_FILE_MESSAGE`)を送出し — 子プロセスは決して生成されません — モデルを `read_file`(外部パスは人間の承認を通る)へ誘導します:

| パターン | 対象 |
| :------- | :--- |
| `(cat\|head\|tail\|less\|more) … /etc/(passwd\|shadow\|sudoers)` | システムのクレデンシャルファイル |
| `(cat\|head\|tail) … .env` | `.env` / `.env.*` の読み取り |
| `cp … .ssh/` | SSH 素材のコピー持ち出し |
| `curl … -d @… .env` | dotenv のアップロードによる持ち出し |
| `(cat\|head\|tail) … ~/.ssh/`、`(cat\|head\|tail) … ~/.aws/` | ホーム配下のクレデンシャルストア |

**これは緩和であり、障壁ではありません。** `dd`、`sed`、`python -c "open(…)"`、`$(< file)`、シェル変数、グロブ、ヒアドキュメントはリテラル正規表現をすべて迂回できます — 本当の読み取り障壁は上記の L2 リードシールド([§2](#2-os-ネイティブサンドボックスバックエンド-l2))であり、承認済みの `sandbox=False` 呼び出しは設計どおりサンドボックス外です。正規表現は明白でよくある試行を止め、モデルを承認フローへ誘導するために存在します。

### 4. 人間が承認するバイパス経路

`sandbox=False` の呼び出しは意図的なバイパス要求です。`HumanInTheLoop` ミドルウェアが動作していて YOLO でない**メインセッション**のグラフでは、`after_model` がその呼び出しを LangGraph の `interrupt()` の上に停めます:

- インタラプトのペイロードは完全なツール呼び出し(ツール名、引数、コマンドまたは query)を示し、`allowed_decisions: ["approve", "reject"]` を含みます。
- **承認**(`{"decisions": [{"type": "approve"}]}`): 元の引数のままで実行されます。環境は依然として洗浄され、cwd はプロジェクトルートに固定され、危険コマンド正規表現も依然として適用されます。承認されたバイパスはスマート承認と危険コマンドの再確認を省きます。人間がこの呼び出し全体を承認したからで、ハードラインのブラックリストはその前にすでに走っています。
- **拒否**(または決定なし): 結果が内容 `User denied: <msg>. <BLOCKED_MESSAGE>` のエラー `ToolMessage` に置き換えられます。コマンドは決して実行されず、2つ目のインタラプトも発生しません。`GraphInterrupt` は飲み込まれずに再送出されます。
- **YOLO モード**(`is_yolo_mode`: `config.yolo_mode`、または `ApprovalMode.OFF`、または環境変数 `SHERRY_YOLO_MODE` が `1` / `true` / `yes`): インタラプトを省き、直接実行します(環境洗浄は依然適用)。
- **バックグラウンド / サブエージェントスコープ**: heartbeat と cron のツールには `caller_scope="background"` がスタンプされ、サブエージェントパイプラインは `caller_scope="subagent"` をスタンプします。それらのグラフには HITL ミドルウェアがないため、ツール層自体が `sandbox=False` を `ToolException` で強制拒否します。そこにインタラプトは存在せず、必要でもありません。

### 5. 外部ファイルパスゲート(ファイルツール)

上記の L1/L2 サンドボックスとは独立に、ファイルツール(`read_file`、`write_file`、`patch_file`、`search_files` など)はすべてのパスを `agent/tools/pub_base/path_utils.py::resolve_external_path()` で解決し、次の6段階のチェックを順に適用します:

1. **`ROOT_DIR` の内側** — 安全なパスとしてそのまま返します。
2. **YOLO 拒否リスト** — セキュリティフロア: `~/.ssh/`、`~/.aws/`、`~/.gnupg/`、`~/.config/gcloud/`、`~/.env`、`~/.gitconfig`、`~/.npmrc`、`~/.pypirc`(`sherry.jsonc` の `yolo_deny_paths` で拡張可能)。ここでヒットした時点で拒否され、以降の層は越えられません: YOLO モード、allowlist のヒット、サブエージェントへの権限継承もすべてこのゲートで止まります。
3. **YOLO モード** — グローバル全許可。パスを返します。
4. **セッション allowlist** — 完全一致パスのエントリとディレクトリエントリ(末尾 `/`、そのディレクトリと配下すべてに一致)はセッション限定で、サブエージェントに継承されます。
5. **事前承認のないサブエージェント** — 拒否: サブエージェントが新しいパスを自己承認することはできません。
6. **メインセッション** — HITL インタラプト、`allowed_decisions: ["approve", "approve_dir", "yolo", "reject"]`:
   - `approve` — このファイルのみ許可(セッション限定、サブエージェントに継承);
   - `approve_dir` — ファイルが属するディレクトリ全体を許可(セッション限定のプレフィックス一致、サブエージェントにも継承);
   - `yolo` — すべての外部パスを恒久的に許可;
   - `reject` — アクセスを拒否。

`resolve_project_path()`(ROOT_DIR 側のフロー)では、パスはファイル I/O の前に3つの構造ゲートを通り、その後のすべてのオープンは最終コンポーネントがシンボリックリンクであることを拒否します(`O_NOFOLLOW`)。このモジュールはモデル可視パスを `ROOT_DIR` を含まない仮想パスとして描画もします。これらの機構と検索コンテインメントフィルタの詳細は §6、ツール実行前にパス引数をスクリーニングする `PathGuard` ミドルウェアは §7 にあります。

### 6. ファイルツールのパスゲート: 3つの構造ゲート、no-follow I/O、仮想パス

ファイルツールはサンドボックスプロセスに依存しません: すべてのプロジェクトパスは `agent/tools/pub_base/path_utils.py` がプロセス内で解決します。`resolve_project_path()` はツールがファイルシステムに触れる前に3つのゲートを順に適用し、拒否されたパスはツールの `except PathOutOfBoundsError` 分岐が §5 の外部パス HITL フローへ回します。

1. **文字列レベルの拒否(`_reject_traversal_input`)。** `~` プレフィックスまたは任意の `..` コンポーネントは、ファイルシステムアクセスの前に `PathOutOfBoundsError` で拒否されます。判定はコンポーネント単位(`Path(file_path).parts`)で、意図的に部分文字列判定にしていません。部分文字列判定は `foo..bar` や `配置..md` のような正当な名前を誤検出するためです。
2. **コンテインメント。** 相対パスはまず `ROOT_DIR` に結合され(`~` は事前展開)、次に `resolve()` が走ります。`resolved != ROOT_DIR and not resolved.is_relative_to(ROOT_DIR)` なら `PathOutOfBoundsError` を送出します。`ROOT_DIR` 自体は許可されます。
3. **シンボリックリンクループ検出(`_raise_if_symlink_loop`)。** `Path.resolve()` はシンボリックリンクループで黙って停止し、ループしているリンクをそのまま返します。解決後のパスがシンボリックリンクなら、`stat()` がその状態を `OSError(ELOOP)`(Linux/macOS、または Windows の `winerror` 1921)に写像して再送出します。後続で不可解に失敗させる代わりに、明示的なエラーにします。

**No-follow I/O(`_open_no_follow`)。** すべての読み書きは `os.open(path, flags | O_NOFOLLOW, mode)` で開くため、パスの最終コンポーネントがシンボリックリンクであってはなりません。解決とオープンの間に差し替えられたリンクは I/O を `ROOT_DIR` の外へリダイレクトできず、TOCTOU ウィンドウが閉じます。拒否は `OSError(ELOOP)` を送出し、Linux/macOS がネイティブに返す errno と同じです。Windows には `O_NOFOLLOW` がないため、ヘルパーは明示的な `path.is_symlink()` チェックにフォールバックし、同じエラーを送出します。`read_file`(読み取り)、`write_file`(書き込み、および `.py` の追記/整形フローの読み取り)、`patch_file`(読み取り + 書き込み)はすべてこれを通ります。

**仮想パス描画。** `to_virtual_path()` は `ROOT_DIR` 配下の実パスを仮想パス(`/src/main.py`)に写像します。`display_path()` は通常その仮想パスを返し、ターゲットがルート外または解決不能(`ValueError` / `OSError` / `RuntimeError` を捕捉)の場合は `real_path.name or "/"` にフォールバックします — したがって `ROOT_DIR` は決して漏れません。`safe_error_detail()` は `OSError.strerror`(例: `Permission denied`)または `UnicodeDecodeError.reason`(`invalid start byte`)のみを返します。それ以外の例外メッセージは意図的に破棄されます。一般的な例外テキストは実際のルートパスを埋め込む可能性があるためです(たとえば `Path.rglob` の途中で送出されたエラー)。残るのは例外型名だけです。正味の効果: モデル可視の結果とエラー詳細に実際のプロジェクトルートは含まれません。

**検索コンテインメント(`_stays_within_root`)。** `os.walk` はディレクトリのシンボリックリンクを辿りませんが、ファイルのシンボリックリンクは一覧に現れます。両検索モードはすべてのヒットを `_stays_within_root(candidate, root)`(`candidate.resolve().relative_to(root.resolve())`、`ValueError` / `OSError` / `RuntimeError` でスキップ)でフィルタするため、検索ツリーの外に解決されるシンボリックリンクは決して返りません — `/etc/passwd` へのファイルシンボリックリンクはスキップされます。検索ルートは常に解決済みです(プロジェクト内検索はさらに `ROOT_DIR` で制限されます)ので、allowlist 済みの外部ディレクトリ検索はそのまま機能します。

**スキャン境界。** 両モードは `TOOLS_TIMEOUTS`(`config/features/agent_side/tools_timeouts.py`)でスキャン自体も制限します: `file_tools_search_time_budget_s`(既定 5.0 秒)の期限切れで走査を停止し、`file_tools_search_max_matches`(既定 10,000)が収集ヒット数を上限化し、`file_tools_search_prune_dirs`(既定 `proc`、`sys`、`dev`)は `dirnames[:]` から除外されるため疑似ファイルシステムには決して降りません。打ち切られたスキャンは決して黙りません — JSON 結果に `scan_truncated: true`、`scan_stop_reason`(`time_budget` / `max_matches`)とヒントが付き、プルーニングが発生した場合は `pruned_dir_count` も付きます。ファイル名パターンは `fnmatch` を通り(ブレース展開なし)、展開数上限は不要です。

**deepagents 参考実装との設計差。** 参考実装はすべてのパスを仮想名前空間(`virtual_mode`)に固定することで、トラバーサルを設計上不可能にします。Sherry は代わりに実ファイルシステムパスを保持し(`prompt_builder`、スキルツール、terminal の cwd がすべて依存)、解決**後**にコンテインメント(上記の3ゲート)を適用し、`O_NOFOLLOW` で TOCTOU を閉じます。`BackendProtocol`、`CompositeBackend`、`StateBackend`、完全な仮想パス名前空間は意図的に採用していません。それはアーキテクチャの書き換えであり、Sherry にマルチバックエンドの用途がないためです。

### 7. `PathGuard` ミドルウェア

**モジュール：** `agent/middlewares/path_guard/core.py` · **クラス：** `PathGuard(AgentMiddleware)` · **フック：** `wrap_tool_call` / `awrap_tool_call` のみ

ファイルツールのゲートは、そこに到達した呼び出ししか守れません。`PathGuard` はメインエージェントチェーンで `ToolCallNormalize` の直後に登録される呼び出し地点のスクリーンです(`agent/core.py`)。リスト順が wrap フックの外側順になるため、`ToolGuardrails` の**内側**で実行されます(`IterationBudget` → `ToolGuardrails` → `PathGuard` → ツール)。拒否は通常のエラー `ToolMessage` として ToolGuardrails に評価され、他のツール失敗と同じ扱いになります。worker / サブエージェントパイプラインには登録しません — 子ツールは自前のゲートを保ち、サブエージェントの外部アクセスはそもそも強制拒否です。

スクリーニングは意図的に保守的です：

- 引数名 `file_path` / `path` / `directory` / `dir` の文字列値のみを検査し、`scheme://` 形式の URL はスキップするため、非パス意味論をファイルシステムパスと誤読しません;
- `..` トラバーサルコンポーネントは共有の `has_traversal_component` 述語で拒否します — URL デコードとバックスラッシュ正規化を先に行うため、`%2e%2e` や `..\` はすり抜けられません。ドットのみのコンポーネント(`...`)もトラバーサルとして扱われます;
- `resolve_project_path()` が受け入れる値はそのまま通します;
- `ROOT_DIR` の外に解決される値は、ハード拒否フロアに当たらない限り通します: `_SYSTEM_DENY_PATHS`(`/etc/passwd`、`/etc/shadow`、`/etc/sudoers`)または YOLO 拒否リスト(`_is_yolo_denied`);
- それ以外の外部パスはツール自身の `resolve_external_path()` HITL フローに委ねます — ミドルウェアは引数を書き換えず、インタラプトも発生させないため、1回の呼び出しは承認の決定をちょうど1つだけ生みます(ツールは実行時に同じゲートを再実行するため、ここで介入すると決定が二重になります);
- 存在しない / 解決不能なターゲットと未知の例外クラスはツールに素通しし、エラーの表面化はツール自身に委ねます。

拒否時、`PathGuard` は警告を記録し、ツールを実行せずに構造化エラー `ToolMessage`(`status="error"`、元の `tool_call_id` とツール名を保持)を返します。

**第二の防衛線。** 4つのファイルツールは自前の `resolve_project_path()` / `resolve_external_path()` 呼び出しを保持しており、コード上で `# redundant: path_guard middleware handles this — kept as the second line of defense` と注記されています(`read_file`、`write_file`、`patch_file`、`search_files`)。ミドルウェアは自前のチェックを忘れたツールを拾う外側のスクリーンであり、ツールごとのゲートが引き続き権威で、外部パスは従来どおり人間の承認フローを通ります。ミドルウェア側の詳細: [Middlewares README §PathGuard](../../agent/middlewares/README.ja.md#pathguard)。

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

`agent/tools/pub_base/sandbox.py` 由来の権威ある表で、`tests/agent/tools/test_sandbox_matrix.py` がセルごとにテストします:

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

### `SHERRY_DENY_READ_PATHS`

```bash
# .env またはシェル環境変数(os.pathsep 区切り; ~ は展開)
SHERRY_DENY_READ_PATHS="~/.kube:~/.config/gcloud"
```

両 OS バックエンドのリードシールドリストにパスを追加します(上記の既定リストは常に含まれます)。この変数は `wrap()` 呼び出しごとに読み込まれます。

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
- **`python_repl` には機密ファイル正規表現がありません。** 上記の terminal 専用ゲートはそれをカバーしません。代わりにラッパースクリプトがビルトインを制限します(安全なサブセットは `open` / `__import__` を含みません) — 別の、より狭い制御です。
- **降格経路は設計どおりサンドボックスなしで実行されます。** `auto` + バックエンドなし = 警告1件を記録してから普段どおりサンドボックスなしで実行。これは意図された「可用性優先」の選択で、逆が必要なら `SANDBOX_POLICY=required` を選んでください。
- **環境変数洗浄は名前ベースです。** ブロック対象の部分文字列を1つも含まない名前(かつ拒否リストにない名前)で保存されたシークレットはそのまま通ります。値のスキャンも動的シークレット検出もなく、それは意図的なものです。
- **ネットワークサンドボックス、seccomp、AppArmor プロファイルは主張も設定もしていません。** 分離は上に示した bwrap / Seatbelt の構築そのものだけです。
