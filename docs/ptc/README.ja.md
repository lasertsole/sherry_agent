# 🧰 プログラム的ツール呼び出し（PTC）

[**English**](README.md) · [中文](README.zh.md) · [한국어](README.ko.md) · **日本語**

> 本稿は[サブエージェント設計ページ](../subagent/README.ja.md)（`execute_code` を executor 専用にする二軸ロールモデル）、[サブエージェントシステム README](../../agent/tools/subagent/README.ja.md)（ロール表に本ツールを載せるランタイムと API のリファレンス）、そして[ツールサンドボックスページ](../sandbox/README.ja.md)（`terminal`、`python_repl`、PTC の `execute_code` が解決する隔離スタック）の設計レイヤの姉妹編です。本ページは、PTC とは何か、スクリプトがなぜ別プロセスで実行されるのか、どのように実ツールへ到達するのか、この封じ込めが実際に何を保証するのか、そしてどこで OS サンドボックスより正直に弱いのかを記録します。

事実の出典：`agent/tools/ptc/**`、`config/features/agent_side/ptc.py`、`config/features/agent_side/tools_timeouts.py`、`agent/tools/subagent/types/functional_role.py`、`agent/tools/subagent/spawn/core.py`、`agent/tools/subagent/spawn/system_prompt.py`、`agent/tools/pub_base/env_scrub.py`、`agent/tools/pub_base/sandbox.py`、`tests/agent/tools/ptc/**`。以下の記述はすべてこのコードに対して検証済みです。

## 目次

- [概要](#-概要)
- [設計根拠](#-設計根拠)
- [プロトコルと成果物](#-プロトコルと成果物)
- [セキュリティモデル](#-セキュリティモデル)
- [設定](#-設定)
- [制限](#-制限)
- [失敗モード](#-失敗モード)
- [テストマップ](#-テストマップ)
- [関連ドキュメント](#-関連ドキュメント)

## 🎯 概要

プログラム的ツール呼び出し（PTC）は `execute_code` ツールの背後にある能力です。モデルが Python スクリプトを 1 本書き、そのスクリプトが生成されたラッパーを通じて Sherry の実ツールを同期的に呼び出します。対象は、ターンごとのツールループが苦手とする形——間に処理ロジックを挟む 3 回以上のツール呼び出し、ループや分岐の形をしたツール利用、モデルのコンテキストに入る前に絞り込みたい大きなツール出力——です。

| 問い | 答え |
|------|------|
| 誰が呼べるか？ | `executor` 機能ロールのみ。ゲートは `PTC_ROLES` であり、メインエージェントも他のどの機能ロールも本ツールを受け取りません |
| スクリプトはどこで動くか？ | 別の Python 子プロセス内。呼び出しごとに生成され、独自のプロセスグループを持つため、タイムアウトはグループごと殺します |
| スクリプトはどうツールへ到達するか？ | `from sherry_tools import ...`——生成されたスタブ。各ラッパーは親の RPC サーバーへ loopback TCP で送る改行区切り JSON リクエスト 1 本です |
| ツールは誰のセッションで動くか？ | 子プロセス自身の `child_session_key`。ツール構築時に束縛され、すべての RPC ディスパッチに注入されます——スクリプト引数になることはありません |
| 何が返るか？ | JSON エンベロープ 1 つ。`status`（`ok` / `timeout` / `error` / `budget_exceeded` / `sandbox_unavailable`）、`output`、`error`、`exit_code`、`tool_calls_made` を含みます |

スクリプトは通常の Python に生成ラッパーを足したもの——async もコンテキストオブジェクトもなく、ツールごとに 1 回の同期呼び出しです：

```python
from sherry_tools import json_parse, read_file

raw = read_file("README.md")
print(json_parse(raw)["total_lines"])
```

```text
executor child agent
  └─ execute_code(code)
       └─ run_ptc: RPC listener + temp dir + sherry_tools.py stub + wrapper script
            └─ python child process  (restricted builtins, scrubbed env, own process group)
                 └─ from sherry_tools import read_file
                      └─ one-shot TCP to 127.0.0.1:<ephemeral port>
                           └─ parent event loop → real BaseTool.ainvoke(session_id=<child key>)
```

## 🧠 設計根拠

**プロセス内 `exec` ではなく別プロセス。** 「制限付き builtins」と「スクラブ済み環境」は、インタプリタが使い捨てであるときに初めて意味を持ちます。親は子に縮小した `__builtins__` 名前空間を入れ、SIGKILL できるプロセスへ `env=scrub_env()` を渡します。ウォールクロック予算には殺せるプロセスグループが必要で、偶発的な無限ループ、クラッシュ、fork 爆弾は子の中に留まります。プロセス内 `exec` は親のグローバルを共有してしまい、そのようには終了できません。

**ツールオブジェクトではなくブリッジ。** 実ツールは非同期 `BaseTool` であり、親のイベントループ、プロバイダ設定、状態解決のためのセッションを必要とします——使い捨てインタプリタに置くべきものの正反対です。生成スタブは引数をソケットで送るだけにし、親は各リクエストを `asyncio.run_coroutine_threadsafe` で自分のループへディスパッチするため、ブロックする子が親の非同期処理を止めることはありません。

**`python_repl` との分業。** 両サーフェスはミラーされた制限付き builtins 集合と同じ `scrub_env` でモデル作成の Python を実行し、どちらも子の開始ディレクトリを `ROOT_DIR` にします。形は異なります。`python_repl` は 30 秒予算でツールアクセスのない 1 スニペットを実行し、`execute_code` はより長いスクリプト（120 秒予算）を実行して最大 50 回の実ツール呼び出しを同期的に行えます。この追加の到達範囲こそが PTC を executor 専用にし、プロセスグループ、RPC ブリッジ、呼び出し予算、インポート許可リストを加える理由です。executor ロールは `python_repl` を保持したままで、PTC は置き換えではなく追加です。両者は同じ OS ネイティブサンドボックスバックエンドも解決します：PTC の子 argv は `sandbox=True` の `python_repl` 呼び出しとまったく同じように包まれ（[ツールサンドボックスページ](../sandbox/README.ja.md)参照）、`auto` の降格と `required` の拒否の意味論は[制限](#-制限)に記します。

**ツール面のゲートは一つの名前付き集合。** `PTC_ROLES`——`agent/tools/subagent/types/functional_role.py` にある `executor` を含む唯一の集合——を、注入点（`spawn/core.py`、ロールの allow/deny ポリシーの後）とプロンプトビルダー（`spawn/system_prompt.py`、PTC ガイダンス節 1 つ）の両方が読みます。したがってツール面とそのプロンプト節は乖離できず、executor のロール定義ファイルは PTC 固有の配線を持ちません。

**フェイルクローズのツール集合。** `build_ptc_tool` は子が利用できるツールを許可リストでろ過し、`execute_code` 自身を取り除きます。executor が持たないツールを列挙しても無害で、実際に執行されるのは積集合です。また `sessions_spawn` / `sessions_yield` / `sessions_kill` / `sessions_steer` / `memory` / `skill_manage` / `question` は決して候補になりません。

## 🔌 プロトコルと成果物

5 つのモジュールがそれぞれ 1 つの責務を担います：

| 成果物 | 責務 |
|--------|------|
| `tool.py` | `ExecuteCodeTool`（`execute_code`）を定義し、子セッションを束縛し、`available_tools` と `ptc_allowed_tools` の積を取るほか、生きたツール schema からモデル向け説明を描画します |
| `runner.py` | `run_ptc` が 1 回の呼び出しを統括します：RPC サーバー起動、一時ディレクトリ作成、`sherry_tools.py` 生成、ラッパースクリプト書き出し、ワンタイム RPC トークン生成、子 argv の OS ネイティブサンドボックスバックエンドによるラップ、子の起動、出力の取得と切り詰め、そして後片付けです |
| `rpc_server.py` | loopback TCP リスナー：1 行 1 JSON のリクエストを解析し、各フレームを実行ごとのトークンで認証し、スクリプトごとの呼び出し予算を執行し、各呼び出しを親イベントループへディスパッチします |
| `stub_generator.py` | 各ツールの `tool_call_schema` から `ToolStub` 仕様を導出し、`sherry_tools.py` モジュールを描画します——ツールごとに同期ラッパー 1 つと、ローカルヘルパーです。RPC 呼び出しはトークンを埋め込まず `SHERRY_PTC_RPC_TOKEN` から読み取ります |
| `builtins.py` | 子の縮小 builtin 名前空間とインポート許可リストを所有し、両方をラッパースクリプトへ描画します |

ワイヤプロトコルは双方向とも 1 行 1 JSON オブジェクトです：

```json
{"tool": "read_file", "args": {"file_path": "README.md"}, "token": "<ワンタイムトークン>"}
{"ok": true, "result": "..."}
{"ok": false, "error": "PTC call budget exhausted", "code": "PTCCallBudgetExceeded"}
```

**シグネチャの忠実性。** スタブのパラメータは `args_schema` ではなく `tool_call_schema` から取るため、フレームワークが注入する引数（`session_id` など）が呼び出し可能なパラメータとして現れることはありません。必須フィールドは必須パラメータとして描画され、任意フィールドはデフォルトを Python リテラルとして描画し、リテラルで表せないデフォルトは `None` にフォールバックします——いずれにせよ実ツールが呼び出しごとに再検証します。生成モジュールは RPC ブリッジに触れない 3 つのローカルヘルパーも持ちます：`json_parse`（寛容な `json.loads`）、`shell_quote`（`shlex.quote`）、`retry`（指数バックオフ）です。

**成果物とライフサイクル。** 呼び出しごとに新しい `sherry_ptc_*` 一時ディレクトリが作られ、`sherry_tools.py` とラッパースクリプトが入ります。子への唯一のパス追加は `PYTHONPATH=<tmpdir>` です。子環境はさらに実行ごとの RPC トークン（`SHERRY_PTC_RPC_TOKEN`）を運び、生成スタブがそれを読み取って毎リクエストで送信します。トークンは `secrets.token_urlsafe` で生成され、メモリ上だけに存在し、`sherry_tools.py` やその他のファイルには決して書かれません。バックエンドが使える場合、子 argv はさらに `backend.wrap([sys.executable, script_path], env)` で包まれます（[ツールサンドボックス](../sandbox/README.ja.md)のサンドボックス経路）。プロセスは依然として `start_new_session=True` で起動されるため、タイムアウトはグループ全体を SIGKILL します。ラッパーはユーザースクリプトの stdout と stderr をバッファへ取り込み、実 stdout へ JSON エンベロープを 1 つ出力し、runner がそれを `status` フィールドへ分類します。`finally` ブロックは RPC サーバーを停止し、スレッドを join し、一時ディレクトリを削除し、子が生き残っていた場合は再度殺します。

**子セッション。** `spawn/core.py` はその run の `child_session_key` をツールのセッションとして渡し、`rpc_server.py` は runnable config を通じて各 `ainvoke` 呼び出しへ注入します。スクリプトはセッションを提供も偽装もできません：`session_id` はそもそもツールパラメータではありません。

## 🔒 セキュリティモデル

以下の制御は能力の削減であり、OS 境界ではありません——正直な位置づけは[制限](#-制限)に記します。

1. **環境スクラビング。** 子の環境は `scrub_env()` から始まります：名前に `KEY`、`TOKEN`、`SECRET`、`PASSWORD`、`CREDENTIAL`、`PASSWD`、`AUTH`、`DSN`、`WEBHOOK`、`BEARER`、`APIKEY`（大文字小文字を問わない）を含む変数は落とされ、Sherry 自身の `*_API_KEY` 変数は明示的に拒否され、`PATH` などの重要名は完全一致優先で保持されます。その後 runner は `PYTHONPATH`、`PYTHONUNBUFFERED`、そして実行ごとの RPC トークン（`SHERRY_PTC_RPC_TOKEN`）だけを追加します。
2. **制限付き builtins。** ラッパーは縮小した `__builtins__` でユーザーコードを実行します：`open`、`exec`、`eval`、`compile`、`globals`、`locals`、`input`、`breakpoint` は存在せず、`exit`、`quit`、`help` も同様です。`print`、コレクション型、`getattr` 系の内省ヘルパーは残ります。
3. **ガード付きインポート。** `__import__` は丸ごと削除されません——インポートバイトコードがそれを必要とするからです——が、許可リストだけを解決するガードに置き換えられます（`sherry_tools`、`json`、`re`、`math`、`time`、`csv`、`datetime`、`collections`、`itertools`、`functools`、`statistics`、`string`、`textwrap`、`decimal`、`random`、`fractions`、`heapq`、`bisect`）。`import os` は `ImportError` を投げ、`json` と `sherry_tools` は通ります。
4. **スクリプトごとのツール呼び出し予算。** RPC サーバーは 51 回目の呼び出しを拒否します：予算チェックはディスパッチ前に走り、超過は `PTCCallBudgetExceeded` を投げ、エンベロープの status は `budget_exceeded` になります。未知のツール名は予算を消費せずに拒否されます。
5. **ウォールクロックタイムアウトとプロセスグループ殺害。** 子は `ptc_timeout_seconds`（120 秒）の下で走ります。期限切れで runner はプロセスグループ全体へ SIGKILL を送り（`start_new_session=True`）、孫プロセスも共に死にます。その後パイプに残る出力を取得し `status: "timeout"` を報告します。
6. **出力上限。** stdout は 50 KB、stderr は 10 KB で切り詰められ、それぞれ明示的な `...[truncated N bytes]` マーカーが付いてからエンベロープがモデルへ届きます。
7. **loopback 限定 RPC とワンタイムトークン。** リスナーは `127.0.0.1` の一時ポートに束縛します。コンストラクタは `0.0.0.0`、`::`、空ホストを即座に拒否し、束縛済みアドレスは子へ渡す前に再確認されます。各リクエストフレームは、子が `SHERRY_PTC_RPC_TOKEN` から読む実行ごとのトークンも運ぶ必要があります。サーバーは定数時間で比較し、トークンが無い／誤っている場合は接続を即座に落とし、応答も返しません。したがって無関係なローカルプロセスはツールを呼ぶことも、推測が近いかどうかを知ることもできません。
8. **子セッション分離。** ディスパッチされる各ツール呼び出しは runnable config に子の `session_id` を載せるため、ツールは子セッションに対して状態を解決します。親セッションが PTC 経由で露出することはありません。
9. **再帰防止と特権除外。** `execute_code` は自身の許可リストに無く、spawn、memory、スキル管理、質問の各ツールは設定により除外されます——スクリプトはサンドボックス内からエージェントを spawn したり、スキルを管理したり、ユーザーに問い合わせたりできません。
10. **メインエージェント不可視。** `_MAIN_TOOLS_BUILDERS` に PTC ビルダーは登録されていません。ツールは子エージェントの組み立て時にのみ、ロールが `PTC_ROLES` に属するときにだけ構築されます。

## ⚙️ 設定

| キー | 既定値 | 管轄 |
|------|--------|------|
| `ptc_timeout_seconds` | 120 | 1 スクリプトのウォールクロック予算、およびツール呼び出し 1 回のディスパッチ窓 |
| `ptc_max_tool_calls` | 50 | 1 スクリプトが予算エラーまでにディスパッチできるツール呼び出し数 |
| `ptc_max_stdout_bytes` | 50000 | 切り詰め前に子から保持する stdout バイト数 |
| `ptc_max_stderr_bytes` | 10000 | 切り詰め前に子から保持する stderr バイト数 |
| `ptc_allowed_tools` | `read_file`、`write_file`、`patch_file`、`terminal`、`search_files`、`web_search` | 子の利用可能ツールと積を取る許可リスト |

オブジェクトは `config/features/agent_side/ptc.py` にあります。`config/features/agent_side/tools_timeouts.py` の共有ツール別レジストリも同じ 120 秒の既定値で `ptc_timeout_seconds` を宣言していますが、runner のタイムアウトは `PTC` オブジェクトから取ります。PTC は pip 依存を一切追加しません：実装は標準ライブラリと、既存の LangChain・loguru だけです。

## ⚠️ 制限

- **それ自体は OS レベルのサンドボックスではありません。** 制限付き名前空間とインポートガードが縛るのは、文字どおりのスクリプトが名指しできるものだけです。しかし `getattr` をはじめとする内省 builtins は残るため、本気のスクリプトは到達可能なオブジェクトをたどって文字どおりの表面を越えた能力に届きます。これは制限付き名前空間の層——既存の `python_repl` ラッパー builtins と同じ層——です。その上で、使えるバックエンドが存在するときは、子 argv が `terminal` と `python_repl` の解決する同じ OS ネイティブサンドボックスバックエンド（Linux の bubblewrap、macOS の Seatbelt）で包まれ、それらの書き込み封じ込めと機密パス読み取りシールドを得ます。ただし正直な注意が 2 つ残ります：`SANDBOX_POLICY=auto`（デフォルト）で使えるバックエンドが無い場合、実行はちょうど 1 件の loguru 警告の後にサンドボックスなしへ降格します。また bwrap 構築は `--unshare-all` を渡すためネットワーク名前空間も非共有化され、実際の bwrap の下で loopback RPC ブリッジが到達可能かどうかは実機の Linux では検証されていません。バックエンド自体は[ツールサンドボックス](../sandbox/README.ja.md)を参照してください。
- **タイムアウトのキャンセルはベストエフォートです。** ツール呼び出しがディスパッチ窓を超えると、サーバーは保持する future をキャンセルしますが、親ループで実行を始めたコルーチンは完走し得ます。スクリプトはタイムアウトエラーを受け取り、ツールの副作用は続く可能性があります。
- **呼び出しごとに起動コストを払います。** ウォームプールはありません：`execute_code` のたびに一時ディレクトリ、スタブ生成、RPC スレッド、新しいインタプリタを立ち上げ、すべてを解体します。取るに足らない 1 回の呼び出しにもプロセス起動が付きます。
- **子の作業ディレクトリはリポジトリルートです。** runner は子の `cwd` を `ROOT_DIR` に既定し、PTC ツールはサブエージェントの spawn された作業ディレクトリを転送しません。そのためスクリプト内の相対パスは executor の spawn 位置ではなくリポジトリルートから解決されます。
- **出力上限は設計上有損です。** stdout 50 KB あるいは stderr 10 KB を超えると末尾はバイト数マーカーに置き換わり、大きな表を出力したスクリプトは切り詰められた版しか得られません。
- **報告はエンベロープ依存です。** ラッパーがエンベロープを出力する前にインタプリタが失敗すると、解析できる構造化出力はありません。runner は生の stderr をエラーとして返し、stdout は空になり得ます。
- **RPC トークンはクロスユーザー境界ではありません。** リスナーはワンタイムトークンを要求するようになり、それを読めないローカルプロセスはツールを呼ぶこともリクエストを完了することもできません。しかしトークンは子の環境を通るため、スクリプトの短いライフタイム中に `/proc/<pid>/environ` を読める*同一ユーザー*のプロセスは依然として回復できます。このトークンが防ぐのは無関係なローカルプロセスであり、同一ユーザーの攻撃者ではありません。リスナーも依然として loopback 限定で、1 スクリプトの実行時間だけ生きます。

## 🧯 失敗モード

| 失敗 | 観察されるもの | 縮退 |
|------|----------------|------|
| 予算超過 | `status: "budget_exceeded"`、エラーに `PTCCallBudgetExceeded` | 以後の呼び出しは失敗し続けます。スクリプトはエラーを捕捉でき、親ターンは部分出力を受け取ります |
| ウォールクロックタイムアウト | `status: "timeout"`、SIGKILL による負の `exit_code` | プロセスグループは死に、部分的な stdout と stderr が上限まで取得されます。親ターンは通常どおり続行します |
| `required` なのにサンドボックス利用不可 | `status: "sandbox_unavailable"`、エラーに `SANDBOX_POLICY=required` | 子を 1 つも spawn する前に拒否されます。親ターンは bubblewrap の導入 / `SANDBOX_POLICY=auto` 設定という実行可能なヒントを得ます |
| 出力切り詰め | `output` または `error` が `...[truncated N bytes]` で終わる | スクリプトは完走しており、モデルへ返るコピーだけが切り詰められます |
| ツールエラー | RPC 応答 `{"ok": false, "error": "TypeError: ..."}` | スタブが `RuntimeError` を投げます。スクリプトは捕捉でき、未捕捉ならエンベロープの `error` に入ります |
| インポート拒否 | `ImportError: import of 'os' is not allowed in PTC` | スクリプトは捕捉でき、未捕捉ならエンベロープの `error` に入ります |
| RPC 切断 | スタブが `RuntimeError: PTC RPC connection closed before a response arrived` を投げる | サーバーは `accept` に戻り、再接続する子を処理します。再試行しないスクリプトはエラーエンベロープで失敗します |
| 未知のツール名 | RPC 応答 `{"ok": false, "error": "unknown tool: ..."}` | 予算を消費せずに拒否されるため、スクリプトは別のツールへフォールバックできます |
| インタプリタレベルの失敗 | エンベロープ無し。生の stderr が `error` になり `status: "error"` | stdout は空になり得ます。モデルは構造化レポートではなくインタプリタ自身のメッセージを見ます |

## 🗺️ テストマップ

| 領域 | テスト |
|------|--------|
| ツール面：同一性、許可リストとの積、束縛セッション、再帰防止 | `tests/agent/tools/ptc/test_tool.py` |
| スタブ生成：実 schema とのシグネチャ一致、注入引数の除外、ローカルヘルパー | `tests/agent/tools/ptc/test_stub_generator.py` |
| RPC プロトコル：loopback 拒否、JSON 往復、予算、ツールエラー、再接続、停止 | `tests/agent/tools/ptc/test_rpc_server.py` |
| RPC トークン握手：正解 / 欠落 / 誤りの三態、拒否時の即時接続クローズ | `tests/agent/tools/ptc/test_rpc_auth.py` |
| サンドボックスポリシーマッピング：バックエンドラップ、`required` 拒否、`auto` 降格と警告 1 件、`off` | `tests/agent/tools/ptc/test_sandbox_integration.py` |
| runner 強化：実行ごとのトークン一意性、生成スタブにトークンが無いこと、子 argv へのバックエンドラップ適用、唯一の降格警告 | `tests/agent/tools/ptc/test_runner_hardening.py` |
| 子プロセス：エンドツーエンドのツール呼び出し、制限付き builtins、インポートゲート、タイムアウト殺害、切り詰め、環境スクラビング、一時ディレクトリ掃除 | `tests/agent/tools/ptc/test_runner.py` |
| ロールグリッド：executor 注入、残り 4 ロール、プロンプトガイダンス、メインレジストリ分離 | `tests/agent/tools/ptc/test_integration.py` |
| 実 LLM エンドツーエンド：executor 子が `execute_code` を選び RPC 経由でファイルを読む | `tests/agent/tools/subagent/test_ptc_executor_e2e.py` |

PTC の 5 スイートは密閉グループ（`unit`、`integration`、`module`）で走ります。エンドツーエンドのファイルだけが唯一の `llm_e2e` テストで、既定では選択解除され、専用の実 LLM ジョブで実行されます。検証するのは実際のモデル経路です——子は `execute_code` を呼ばねばならず、取得したエンベロープは RPC 呼び出しのディスパッチを報告せねばならず、出力された行数はテスト時にファイルから計算した数と一致せねばなりません。サンドボックスに `open` が無いため、`read_file` を通さずにこの数は作れません。

## 🔗 関連ドキュメント

| ページ | 扱う内容 |
|--------|----------|
| [サブエージェント設計](../subagent/README.ja.md) | 二軸ロールモデル（深さロール × 機能ロール）と、`execute_code` が executor 専用である理由 |
| [サブエージェントシステム README](../../agent/tools/subagent/README.ja.md) | ランタイムと API のリファレンス：spawn パイプライン、ツール一覧を含むロール表、レジストリの挙動 |
| [ツールサンドボックス](../sandbox/README.ja.md) | 環境スクラビングと、`terminal`・`python_repl`・PTC の `execute_code` が解決する OS ネイティブ隔離——PTC が継承する `auto` 降格と `required` 拒否を含む |
