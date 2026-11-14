# 🐞 デバッグループ：ブラウザ捕捉と LSP 編集時診断

[**English**](README.md) · [中文](README.zh.md) · [한국어](README.ko.md) · **日本語**

> コーディング作業のための 2 つのループです。ブラウザはページで実際に起きたこと（console、未捕捉例外、失敗リクエスト）を記録し、reload 後の原因究明を推測ではなく事実にします。言語サーバー層は、編集ツールが書いた内容とサーバーが見ている内容を一致させ、新しいエラーを次のターンに提示し、finish ゲートが再生できる診断証拠を書き込みます。

**正確な情報源：** `server/service/browser_manager.py`（`_BrowserDebugCaptureMixin`）、`agent/tools/browser/tools/browser_console.py`、`agent/tools/browser/tools/browser_network.py`、`agent/tools/browser/tools/browser_merged.py`、`agent/tools/code_intel/lsp/client.py`、`agent/tools/code_intel/lsp/manager.py`、`agent/tools/code_intel/lsp/diagnostics.py`、`agent/tools/pub_base/lsp_sync.py`、`agent/tools/pub_base/regression_gate.py`、`agent/middlewares/reminder_catalog/sources.py`、`config/features/infra_side/browser_agent.py`、`config/features/agent_side/lsp.py`。

## 🌐 ブラウザ console と network の捕捉

任意参加のブラウザ機能が有効なとき、通常の各ページは `Runtime.enable`、`Network.enable`、`Log.enable` も有効にします。マネージャーは次をバッファします：

| イベント | バッファ内容 | 補足 |
| :--- | :--- | :--- |
| `Runtime.consoleAPICalled` | レベル + 可読な引数テキスト | `RemoteObject` の value / description / preview |
| `Runtime.exceptionThrown` | エラー + 最大 10 フレームのスタック | バッファ内の行・列は 1 始まり |
| `Log.entryAdded` | ブラウザログ | `warning` は `warn` に正規化 |
| `Network.responseReceived` | status 400 以上のみ | url / status / 種別 / mime |
| `Network.loadingFailed` | 失敗 + 理由 | url は `requestWillBeSent` の対応表から補完 |

header、cookie、body は読みません。各バッファは `debug_buffer_max`（200）件までで、メインフレームの `Page.frameNavigated` と明示的な navigate / back / forward で消去されます。

モデルは統合ツールから読みます：`browser(action="console", level="error")` は console 出力と未捕捉例外、`browser(action="network", min_status=400, include_failures=true)` は失敗リクエストを返します。`SHERRY_BROWSER_DEBUG_CAPTURE=0` ではドメイン自体を有効にせず、両 action は無効であることを明示します。

## 🧠 LSP 編集時の一貫性

以前の `lsp_diagnostics` は最初の `didOpen` の後サーバーのキャッシュを読むだけでした。エージェントの編集後もサーバーは古い本文を持っていたのです。現在は 4 段階で閉じています：

1. **編集同期。** `LSPClient.sync_document` は初回に `textDocument/didOpen`、以降は全文の `didChange` と `didSave` を送ります。`patch_file`、`write_file`、`lsp_format(write=true)`、直接ファイル指定の `ast_grep_rewrite` が `agent/tools/pub_base/lsp_sync.py` を呼び、**すでに acquire 済み** のサーバーにだけ送ります（`LspServerManager.peek`）。編集が言語サーバーを起動することはありません。同一内容は送りません。
2. **新鮮な診断。** `lsp_diagnostics` はディスクからサーバーを更新し、観測した世代より新しい `publishDiagnostics` を待ちます（`diagnostics_generation` / `wait_diagnostics_since`）。ファイルが変わっておらずキャッシュがあれば、来ない再送を待たず即答します。
3. **受動注入。** `agent/tools/code_intel/lsp/diagnostics.py` がファイルごとに `publishDiagnostics` を集約し、セッションごとに配信を記録します。`lsp_diagnostics` reminder source（priority 84、transient、error のみ、10 ファイル / 20 件まで）が各ターン最初の model call 前に未配信エラーを注入します。診断世代ごとに 1 回で、新しい publish が配信を再開します。
4. **finish の証拠。** 同じ編集経路がデバウンス（`AUTO_DIAGNOSTICS_DEBOUNCE_S`）付きでバックグラウンド診断を 1 回走らせ、証拠行（`kind="lsp_diagnostics"`、`auto=true`、pass/fail）を追加します。`taskflow_flow(action="finish")` の regression gate は shell 許可リストではなく LSP client でこれらの行を再生します：pass → `unmark`、fail → `broken`、サーバー無し/タイムアウト → スキップ。編集で入った型エラーは finish 時に `[broken]` として現れます。

9 番目の LSP ツールは `lsp_hover`（`textDocument/hover`、MarkupContent をテキスト化）です。堅牢性：クラッシュしたサーバーは最大 `lsp_max_restarts`（3）回まで再起動し、その後ツールは tree-sitter / grep に降格します。`-32801` ContentModified は 500/1000/2000 ms のバックオフで再試行します。

## ⚙️ 設定

| 環境変数 | 既定 | 効果 |
| :--- | :--- | :--- |
| `SHERRY_BROWSER_DEBUG_CAPTURE` | 1 | console/network/log ドメインを有効化 |
| `AUTO_DIAGNOSTICS_ENABLED` | 1 | 自動診断の証拠行を書き込む |
| `AUTO_DIAGNOSTICS_MAX_FILES` | 20 | セッションごとの自動検査ファイル数 |
| `AUTO_DIAGNOSTICS_TIMEOUT_S` | 5 | バックグラウンド診断 1 回の予算 |
| `AUTO_DIAGNOSTICS_DEBOUNCE_S` | 0.4 | 連続編集をまとめる |
| `LSP_DIAGNOSTICS_INJECT_MAX_FILES` | 10 | ターンごとの注入ファイル数 |
| `LSP_DIAGNOSTICS_INJECT_MAX_ERRORS` | 20 | ターンごとの注入エラー数 |
| `LSP_MAX_RESTARTS` | 3 | 降格前のクラッシュ再起動上限 |
| `LSP_CONTENT_MODIFIED_MAX_RETRIES` | 3 | `-32801` の再試行回数 |
| `LSP_CONTENT_MODIFIED_BACKOFF_S` | 0.5 | 最初のバックオフ（倍々） |

## 🧪 テスト

- `tests/server/service/test_browser_debug_capture.py` —— イベント形状、失敗のみの network、リング上限、ナビゲーション消去、無効スイッチ。
- `tests/agent/tools/test_browser_tools.py` —— `console` / `network` action、整形、無効時の案内。
- `tests/agent/tools/code_intel/lsp/test_edit_sync.py` —— didOpen/didChange/didSave、冪等同期、publish 世代、ContentModified 再試行。
- `tests/agent/tools/code_intel/lsp/test_diagnostics_registry.py` —— セッション別配信、変更検出、ルート絞り込み、上限。
- `tests/agent/tools/pub_base/test_lsp_sync.py` —— 編集同期、デバウンス証拠行、fail-open。
- `tests/agent/tools/pub_base/test_regression_gate.py` —— LSP 行の再生（pass → unmark、fail → broken、サーバー無しはスキップ）。
- `tests/agent/middlewares/test_lsp_diagnostics_reminder.py` —— reminder の描画と 1 回配信。

## ⚠️ 制限

- console/network 捕捉はページ単位で、ページ終了と共に消えます。完全な reload でバッファが消えるのは設計です（「この読み込み以降に何が起きたか」を診断します）。
- 注入するのは error 重要度の診断だけです。warning は `lsp_diagnostics` で取得できます。
- 自動診断はすでに acquire 済みのサーバーがある場合だけ動きます。LSP をまったく使わないプロジェクトは負担ゼロですが、バックグラウンド検査も受けません。
- `ast_grep_rewrite` は直接ファイル引数のみ同期します。ディレクトリ指定の適用は変更ファイルを報告しません。
