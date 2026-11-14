# 🧠 ローカルモデル daemon：Embed、Reranker、ローカル Chat

[**English**](README.md) · [中文](README.zh.md) · [한국어](README.ko.md) · **日本語**

> ローカル GGUF モデル（bge-m3 のベクトル、bge-reranker、補助チャットモデル）は、もうオーケストレーターの中ではロードされません。モデルファミリーごとに独立した子プロセスが重みを一度だけロードし、loopback HTTP JSON で応答し、アイドル時間の後に自分で終了します。メインプロセスが保持するのはリクエストと応答のデータだけです。

**正確な情報源：** `models/daemon/base.py`、`models/embed_model/text/daemon.py`、`models/embed_model/text/core.py`、`models/reranker_model/daemon.py`、`models/reranker_model/__init__.py`、`models/LLMs/local_daemon/server.py`、`models/LLMs/local_daemon/client.py`、`models/LLMs/auxiliary_llm/core.py`、`config/features/infra_side/local_daemons.py`、`packaging/backend_entry.py`、`skills/builtin/core/speech_to_text/scripts/core.py`。以下の各結論はコードと「実測ベースライン」の数値に照らして確認済みです。

## 🎯 なぜ daemon か

バックエンドは**1 つの** Python プロセスです（`server/__main__.py` が `app.config.processes = 1` を固定）。すべてのセッション、ミドルウェアチェーン、WebSocket 配信、ツールサンドボックスがここにあります。600 MB のベクトルモデルや 5.5 GB のチャットモデルをこのプロセスに置くことは、最も応答性が必要なプロセスに最も OOM されやすいコストを負わせることになります。

旧来のプロセス内 embed 経路の実測値：

| 指標（bge-m3 q8_0、本機） | プロセス内（以前） | daemon（以降） |
| :--- | :--- | :--- |
| embed 呼び出しごとのホットロード `Llama(...)` | 約 1.27–1.30 秒 | 0（常駐済み） |
| バッチごとの推論 | 約 0.32 秒 | 約 0.49 秒（loopback HTTP を含む） |
| 呼び出し中のメインプロセス RSS | ピーク約 1017–1055 MB（`ru_maxrss`） | 46.5 → 47.3 MB（`VmRSS`） |
| モデルの RSS | メインプロセス内 | 約 1045 MB（daemon 内） |
| プロセス内ベクトルとの cosine | — | 1.0（完全一致） |

**これはメモリの隔離であり、総量の削減ではありません。** すべてのファミリーが同時に有効な場合、総メモリ（メインプロセス + daemon）はほぼ同じか少し増えます。利点は、メインプロセスに大きなピークが出ないこと、無効な機能は spawn されずメモリを使わないこと、ホット呼び出しが速いこと（GGUF を毎回マップし直さない）です。

## 🏗️ アーキテクチャ

```text
メインプロセス（オーケストレーター）              daemon 子プロセス（detached）
  _LocalEmbeddingBackend.embed(texts)  ─HTTP──▶  POST /embeddings
  _LocalGgufBackend.rank(...)          ─HTTP──▶  POST /rerank | /filter | /predict
  DaemonChatClient.create_chat_...     ─HTTP──▶  POST /v1/chat/completions
                                                 1 つの GGUF を一度だけロード
                                                 推論はロックで直列化
                                                 idle watchdog が期限で終了
```

クライアント側は共通です。`DaemonSpec` が端点、pid ファイル、import 先を持ち、`models/daemon/base.py` が 1 つのライフサイクルを実装します：

- **ready 判定は厳格。** `/healthy` は `{"ready": true}` を返す必要があります。プロセスが生きていてもモデルがロード中なら ready ではありません。
- **spawn は重複しない。** 記録された pid が生きていれば再 spawn しません。古い pid ファイルは置き換えます。
- **ReadyState は 3 値。** `READY`、`WARMING`（プロセスはある、待つ）、`UNAVAILABLE`（プロセスがなく起動もできない）。この区別があるため、クライアントは重複ロードなしでフォールバックできます。
- **kill** は SIGTERM、最大 5 秒待機、SIGKILL、そして自分の子プロセスを回収します。atexit kill は実際に spawn したプロセスだけが登録します。
- **アイドル終了** は `IdleWatchdog`：`*_idle_timeout_s` の間リクエストがなければ `os._exit(0)`。次の呼び出しで再びコールドスタートします。

### 端点、ポート、pid ファイル

| ファミリー | ポート | pid ファイル | 端点 |
| :--- | :--- | :--- | :--- |
| Embed | 9012 | `.embed_daemon.pid` | `GET /healthy`、`POST /embeddings` |
| Reranker | 9013 | `.reranker_daemon.pid` | `GET /healthy`、`POST /rerank`、`/filter`、`/predict` |
| ローカル Chat | 9014 | `.llm_daemon.pid` | `GET /healthy`、`POST /v1/chat/completions` |
| ビジョン Chat | 9015 | `.vision_daemon.pid` | `GET /healthy`、`POST /v1/chat/completions`（mmproj） |
| STT（リファクタ済み） | 9011 | `.stt_daemon.pid` | `GET /healthy`、`POST /transcribe` |

pid ファイルと daemon ログは `APP_HOME`（パッケージ版では書き込み可能なアプリデータ、ソースツリーではリポジトリルート）に置かれ、読み取り専用のコードツリーには書きません。すべての daemon は `127.0.0.1` のみにバインドし、JSON・認証なしで応答します。既存の STT daemon と同じく、ネットワークサービスではなくローカルキャッシュです。

### パッケージング

ソースツリーでは `python -c "from <module> import main; main()"` で起動します。PyInstaller の凍結版は `-c` を実行できないため、`--sherry-daemon <module>` を付けてパッケージ済みバックエンドを再実行します。`packaging/backend_entry.py` は `server.__main__` をインポートする**前に** そのモジュールの `main()` へ分岐するので、daemon 子プロセスはログ、`nest_asyncio`、LangSmith を初期化しません。`--sherry-home` と `--sherry-desktop` も転送され、子プロセスは同じ書き込み可能 home を解決します。

## 🔌 3 つのクライアント

**Embedding**（`models/embed_model/text/core.py`）。daemon は生のベクトルを返し、クライアントが L2 正規化と順序検査を保持します。呼び出し側がすでに作っているバッチ（`memory_embeddings` は 32 件、メッセージ索引は 64 件）をそのまま送るので、索引経路に 2 段目のバッチ処理は不要です。daemon エラーの後に 1 回再試行し（アイドル終了やクラッシュを吸収）、2 回目の失敗は呼び出し側に渡ります（意味検索は fail-open）。

**Reranker**（`models/reranker_model/__init__.py`）。`rank`、`filter`、`predict` はそれぞれ 1 つの端点に対応します。`predict_scores` はペアごとの往復ではなく、ペア列全体を 1 リクエストで送ります。ローカル GGUF バックエンドはそのリクエスト内でもバッチ処理します：最大 8 ペアが 1 回の `llama_encode` を共有し（コンテキストの ubatch トークン予算で制限）、ネイティブのバッチ呼び出しが失敗するとペア単位の経路へフォールバックします。実測 32 ペアで 12.0 s → 8.4 s。バッチカーネルは僅差のスコアを最大 ~0.04 変動させ得ますが、0.5 のフィルタ判定は検証で同一でした。`RERANKER_MODEL_LOCAL=false` のときクラウド側は一切変わりません。

**ローカル Chat とビジョン**（`models/LLMs/auxiliary_llm/core.py` / `models/LLMs/base_local_llama.py`）。ローカル GGUF 経路はネイティブ tool call を**使いません**：`LocalToolBinder` が JSON 指示を注入し、`LocalStructuredOutput` が instructor で `create_chat_completion_openai_v1` をラップします。したがって daemon が公開するのは llama-cpp 自身の `create_chat_completion` 形状で、`models/LLMs/local_daemon/client.py` はその 2 メソッドだけを実装する小さな shim です。`create_chat_completion_openai_v1` は応答を OpenAI `ChatCompletion` に検証し（instructor が必要とする id/object/created/model を補完）、instructor の制御引数は線の向こうへ送りません。daemon が起動できない、または予算を超えても warm でない場合、`_LocalAuxiliaryChatModel` はプロセス内 `Llama` にフォールバックします——機能優先、隔離は次。マルチモーダル基底クラスは同じクライアントをポート 9015 の daemon に使います。`image_url` ブロックはそのまま llama-cpp の Qwen2.5-VL handler に渡ります。

## 🛟 失敗時の動作とスイッチ

| 状況 | 動作 |
| :--- | :--- |
| 呼び出しの間に daemon がアイドル終了 | クライアントが 1 回再試行し、`ensure_ready` が再 spawn |
| daemon が warm 中（モデルロード中） | `WARMING`；embed/reranker は再試行可能なエラー、ローカル chat はプロセス内へフォールバック |
| daemon をまったく起動できない | `UNAVAILABLE`；embed/reranker はそのプロセスの残りで旧来のプロセス内ロードを使用、ローカル/ビジョン chat はプロセス内 `Llama` |
| リクエスト中に daemon がクラッシュ | そのリクエストは失敗；記憶検索は cosine 順を維持し、索引構築はエラーを記録 |
| 機能を無効化 | `EMBEDDING_DAEMON_ENABLED=false`、`RERANKER_DAEMON_ENABLED=false`、`AUXILIARY_LLM_DAEMON_ENABLED=false` で daemon なしの旧経路に戻る |

すべての設定は `LOCAL_DAEMONS`（`config/features/infra_side/local_daemons.py`）にあり、環境変数で上書きできます：ポート（`*_DAEMON_PORT`）、ready 予算（`*_READY_WAIT_S`）、アイドル窓（`*_DAEMON_IDLE_TIMEOUT_S`）、リクエストタイムアウト（`*_HTTP_TIMEOUT_S`）、`AUXILIARY_LLM_N_CTX`、`VISION_LLM_DAEMON_*`。`auxiliary_n_ctx` の既定値は従来の 40 960 です。下げるとローカル `max_tokens` も窓の半分に制限され、小さな KV cache と過大な補完要求が組み合わさることはありません。

## 📏 実測ベースライン（2026-11-11、本機）

| 測定項目 | 結果 |
| :--- | :--- |
| `import server.__main__` | ピーク RSS 約 26.7 MB、約 0.86 秒 |
| `agent.core` + `server.trigger.http` | ピーク RSS 約 153.9 MB、約 5.2 秒 |
| 起動時の import 集合 | `torch`、`transformers`、`cv2`、`mineru_vl_utils`、`leidenalg`、`snkv`、`tree_sitter`、`gguf`、`llama_cpp` はいずれも無し |
| Embed daemon 初回呼び出し（spawn + ロード） | 3.8 秒 |
| Embed ホット呼び出し | 0.49 秒（プロセス内は約 2.88 秒） |
| embed 中のメインプロセス RSS | 46.5 → 47.3 MB |
| daemon RSS | 1045.6 MB |
| ベクトル cosine（daemon とプロセス内） | 1.0 |
| Reranker daemon 初回呼び出し（spawn + 606 MB GGUF ロード） | 32.8 秒 |
| Reranker ホットランク（4 文書） | 1.33 秒（プロセス内は呼び出しごと約 31.5 秒、再ロード込み） |
| rerank 中のメインプロセス RSS | 46.6 → 46.9 MB；daemon 898.4 MB |
| Reranker のスコアと順序（daemon とプロセス内） | 完全一致 |
| ローカル Chat 初回呼び出し（Qwen2.5-0.5B-Instruct Q4、spawn + ロード） | 3.33 秒 |
| ローカル Chat ホット呼び出し | 0.77 秒 |
| ローカル Chat 中のメインプロセス RSS | 65.3 → 65.9 MB；daemon 600.4 MB |
| ローカル Chat の tool call と構造化出力 | `add(a=2,b=2)` を解析；`Answer(answer='Paris')` |
| 同じモデルをプロセス内でロード | メインプロセスのピーク +537.9 MB |
| ビジョン初回呼び出し（Qwen2.5-VL-3B Q4 + mmproj、96 px 画像） | 13.61 秒（CPU: 画像エンコード + prompt） |
| ビジョン ホット呼び出し | 7.44 秒 |
| ビジョン中のメインプロセス RSS | 67.3 → 20.2 MB（ピーク 68.2）；daemon 3 571 MB |
| ビジョンの回答 | `Red`（正解）、ポート 9015 |

**「重い依存の遅延ロード」方向は、実測された Kill criteria でスキップしました：** 完全な起動 import チェーン（モジュールブート + agent core + curator/cron + trigger + services）は `torch` / `transformers` / `cv2` / `mineru_vl_utils` / `leidenalg` / `snkv` / `tree_sitter` / `gguf` / `llama_cpp` のいずれもロードしません。依存自体は小さくなく、単独 import で `torch` 約 490 MB / 5.9 秒、`transformers` 約 41 MB / 9.9 秒、残りは基本環境に未インストールです。つまり既に最適化が望む位置（各機能関数の内側）にあります。`tests/perf/test_boot_import_set.py` がどれか一つでも起動集合に戻ると失敗します。残る起動コストは `openai` / `langchain` / `langgraph` とログ・設定のブートで、すべてのルートが必要とします。（`PIL` はメディア圧縮モジュール経由で起動時に import されますが、実測約 49 ms で RSS ピークの増加は測定不能なため、変更する価値はありません。）

**ビジョン同構。** ITTT/VTTT は**同じ**ローカル chat daemon（ポート 9015）を通ります：daemon は `SHERRY_LOCAL_LLM_MMPROJ_PATH` を読み、`Qwen25VLChatHandler` を追加し、`n_batch = n_ubatch = n_ctx` としてプロセス内 `LocalMultimodalLlamaChatBase` を正確に模倣します。両ファミリーは同じ Qwen2.5-VL の重み + mmproj を解決するため 1 つのビジョン daemon を共有します。別のビジョンモデルには別ポートが必要です。`LocalMultimodalLlamaChatBase` は daemon を先に試し、起動できなければプロセス内クライアントへフォールバックします。安全審査は引き続き補助クライアントを共有します。長い生成の後ろに並んだ審査呼び出しは 20 秒の fail-open 予算に達することがありますが、これは現在と同じ結果です。

## 🧪 テスト

- `tests/models/daemon/test_base.py` —— pid ファイル、ready 3 値、spawn コマンド（ソースと凍結）、atexit、kill/回収、リクエスト復号、アイドル watchdog。
- `tests/models/embed_model/text/test_embed_daemon.py` —— 偽モデル上の実 loopback server：順序、バッチ、検証、health；クライアント：ペイロード、再試行、warm、フォールバック、正規化。
- `tests/models/reranker_model/test_daemon.py` —— rank/filter/predict のワイヤ形状、検証、クライアント再試行とフォールバック。
- `tests/models/LLMs/local_daemon/test_local_daemon.py` —— llama-cpp 引数の転送、instructor 互換 `ChatCompletion`、補助モデルの daemon/フォールバック接続、`n_ctx`/`max_tokens` のクランプ、ビジョン daemon（mmproj handler、ポート 9015）とそのフォールバック。
- `tests/packaging/test_backend_entry.py` —— `--sherry-daemon` の解析と分岐。
- `tests/skills/builtin/core/speech_to_text/test_stt_daemon_pid.py` —— STT リファクタ後も共通ライフサイクル上で同じ pid 契約。
- `tests/config/test_features_infra_side.py` —— `LOCAL_DAEMONS` の既定値、環境上書き、不正値のフォールバック。

## ⚠️ 制限

- ポートごとに 1 モデルインスタンス。2 つの異なる補助 GGUF（2 つ目のローカルプロファイル）は daemon を共有できません。パスは spawn 時に解決され、daemon は最初にロードしたモデルだけを扱います。
- リクエストキューの公平性はありません：`ThreadingHTTPServer` と 1 本の推論ロックにより、待機スレッドの順序は OS のスケジューラ次第です。長いバッチは 1 回の検索を遅らせますが、ローカルキャッシュとしては許容範囲です。
- 認証、TLS、クロスホストアクセスはありません。daemon は STT daemon と同じく、構造上 `127.0.0.1` のみにバインドします。
- ローカル chat daemon はストリーミング非対応です。ローカル補助経路は決してストリーミングしない（呼び出しは `invoke`/`ainvoke` のみ）ため、`stream: true` は黙って降格せず明示的に拒否します。
