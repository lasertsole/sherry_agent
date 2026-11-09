# パッケージ版デスクトップの起動信頼性

[English](README.md) · [中文](README.zh.md) · [**한국어**](README.ko.md) · **日本語**

> Tauri 統合インストーラーが Windows の初回起動を乗り切るための仕組み：同梱設定テンプレートは PyInstaller の凍結ツリーから解決し、QQ SDK は実行時 pip ではなくバックエンドに同梱し、トレイ終了にはウィンドウ権限を追加し、クライアント入力の誤りは 500 ではなく 400 を返します。

## 初回設定のシード

シェルは `--sherry-home <app-data>` を渡します。`config/_home.resolve_code_root()` は**同梱テンプレート**に対して `sys._MEIPASS`（onedir の `_internal`）を優先し、`ROOT_DIR` は従来どおりプロジェクト／スキルのルートとして残ります。`sherry.jsonc`、`.models`、`.network`、`.env` は初回 import 時にそのテンプレートルートからシードされます。テンプレートが見つからない場合は静かにスキップせず warning を出すため、パッケージ構成の異常をログから特定できます。

単一ファイルのテンプレートには追加のパッケージ規則があります：PyInstaller `--add-data` の DEST は**ディレクトリ**です。`.models.example` を DEST にすると同名のディレクトリが作られ、初回シーダーはディレクトリを見てスキップします——モデル/アプリ設定タブが空になる原因です。ビルドは全ファイル項目をバンドルルート（`.`）へ送り、`check_seed_files` はシードテンプレートが通常ファイルでない凍結ツリーを拒否し、`tests/packaging/test_build_backend.py` が両方を固定します。


## 凍結環境と QQ

凍結バックエンドには仮想環境がなく、自身のインタープリタへパッケージをインストールできません。`channels/deps.install_requirements()` と `channels/registry._ensure_deps()` は `sys.frozen` が真のとき短絡します。QQ SDK（`botpy`、パッケージ名 `qq-botpy`）はプロジェクト依存に追加され、`packaging/build_backend.py` が収集するため、プラグインは通常どおり import できます。同梱漏れのビルドでは INFO を 1 行だけ出してチャネルを無効化し、起動のたびに `uv pip install` を繰り返しません。

## トレイ終了の動作

`CloseToTrayDialog` は Tauri IPC 経由で `getCurrentWindow().hide()` を呼びます。`core:default` は読み取り専用のウィンドウ内省だけを許可するため、`client/src-tauri/capabilities/default.json` に `core:window:allow-hide` を明示的に追加します。これがないとダイアログは終了を確認してもウィンドウは表示されたままです。capability は Rust シェルにコンパイルされるため、インストーラーの再ビルドが必要です。

## HTTP クライアント入力エラー

パッケージ版のルート調査で、5 つの handler が意図したクライアントエラーを汎用 500 に漏らしていました。メディアの必須／安全でないパラメータ、履歴ページの欠落／非整数フィールド、pending interrupt の session 欠落、`DELETE /sessions` の JSON body／session 欠落、memory entry id の非整数です。現在は HTTP 境界で共通の `bad_request`／`not_found`（400／404）を返します。

## 検証

対象を絞ったテストは凍結テンプレートルートの解決と 4 つのシードファイル、凍結環境のインストール短絡、QQ SDK の収集、トレイ capability、5 つの 400 応答をカバーします。初回起動のエンドツーエンド確認は、新しい Windows ユーザープロファイルでインストーラーを再ビルドして実施する必要があります。

出典：`config/_home.py`、`config/path.py`、`config/sherry_settings.py`、`config/models_settings.py`、`config/network_settings.py`、`channels/deps.py`、`channels/registry.py`、`plugins/channels/qq/core.py`、`packaging/build_backend.py`、`server/trigger/http/`、`client/src-tauri/capabilities/default.json`。
