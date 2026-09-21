# 🛡️ 128K MAX_TOKEN ガード

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

> エージェントが両方の LLM に 128K コンテキストウィンドウの下限をどう強制するか: 共有の述語が 4 つの適用ポイントで例外を投げ（サーバー起動、グラフ構築、サブエージェントのスポーン、`.env` 書き込み）、フロントエンドはブロックする前に警告し、`GET /model-config` が現在の判定を公開します。

事実の出典: `config/features/agent_side/token_guard.py`、`server/__main__.py`、`agent/core.py`、`agent/tools/subagent/spawn/core.py`、`server/service/env.py`、`server/trigger/http/model_config.py`、`client/app/composables/model-config.ts`、`client/app/composables/use-chat-stream.ts`、`client/app/pages/home/components/ConfigDialog.vue`、そして `.env.example`。本文書中のすべての行番号と定数は、そのコードに照らして検証済みです。

## 目次

- [概要](#-概要)
- [しきい値と設定](#-しきい値と設定)
- [適用、エンドポイント、フロントエンド](enforcement/README.ja.md)
  - [🚧 適用ポイント](enforcement/README.ja.md#-適用ポイント)
  - [🌐 エンドポイント: GET /model-config](enforcement/README.ja.md#-エンドポイント-get-model-config)
  - [💻 フロントエンドの挙動](enforcement/README.ja.md#-フロントエンドの挙動)
- [運用ガイド](#-運用ガイド)
- [テスト](#-テスト)
- [制限と非目標](#%EF%B8%8F-制限と非目標)
- [ファイルマップ](#-ファイルマップ)

## 🎯 概要

このガードは一つの不変条件であり、4 つの独立した場所で強制されます:

> **エージェントが動かす両方の LLM が、少なくとも 128K トークン（`131_072`）のコンテキストウィンドウを確保しなければなりません。**

- **メイン LLM**（`MAIN_LLM_MAX_TOKEN`）は要約のトリガーしきい値と、会話全体の予算を左右します。
- **補助 LLM**（`AUXILIARY_LLM_MAX_TOKEN`）は圧縮、メモリ処理、ツール出力の要約、そしてメイン以外のすべてのサブエージェントロールで使われます。

しきい値はただ一つのモジュール `config/features/agent_side/token_guard.py` に存在します:

```python
MIN_REQUIRED_MAX_TOKEN: int = 131_072


class TokenGuardError(RuntimeError):
    """Raised when a MAX_TOKEN env value is below the minimum."""


def assert_max_token_valid(key: str, value: int | None) -> None:
    """Raise TokenGuardError if value is None or < MIN_REQUIRED_MAX_TOKEN."""
    if value is None:
        raise TokenGuardError(f"{key} is not set; must be >= {MIN_REQUIRED_MAX_TOKEN} (128K)")
    if value < MIN_REQUIRED_MAX_TOKEN:
        raise TokenGuardError(
            f"{key} = {value} is below the minimum {MIN_REQUIRED_MAX_TOKEN} (128K); "
            f"agent startup is blocked"
        )
```

`assert_max_token_valid(key, value)` が `TokenGuardError` を投げるのは、正確に次の 2 ケースだけです:

| 入力 | 結果 |
| :---- | :----- |
| `value is None`（環境変数が未設定または空） | `"{key} is not set; must be >= 131072 (128K)"` を投げる |
| `0 <= value < 131072` | `"{key} = {value} is below the minimum 131072 (128K); agent startup is blocked"` を投げる |
| `value >= 131072` | 通過（`None` を返す） |

上限はなく、下限を下げる方法もありません。`TokenGuardError` は `RuntimeError` のサブクラスで、各呼び出し元が提示方法を決めます: 起動時のハード終了、実行中の WebSocket エラーチャンク、書き込み時の `ValueError` です。

`config/features/__init__.py` がこの 3 つのシンボルを再エクスポートするので、すべての層が同じ場所からインポートします:

```python
from config.features import (
    MIN_REQUIRED_MAX_TOKEN,
    TokenGuardError,
    assert_max_token_valid,
)
```

## 📐 しきい値と設定

| 環境変数 | 意味 | 要件 | `.env.example` の既定値 |
| :------ | :------ | :------- | :--------------------- |
| `MAIN_LLM_MAX_TOKEN` | メインモデルのコンテキストウィンドウ | `>= 131072` | `131072`（`.env.example` 8 行目） |
| `AUXILIARY_LLM_MAX_TOKEN` | 補助モデルのコンテキストウィンドウ | `>= 131072` | `131072`（`.env.example` 59 行目） |

両キーは環境書き込みの許可リスト `TOKEN_KEYS` にも登録されています（`server/service/env.py:27`）:

```python
TOKEN_KEYS = frozenset({"MAIN_LLM_MAX_TOKEN", "AUXILIARY_LLM_MAX_TOKEN"})
```

### 有効値の解決

4 つの適用ポイントはすべて同じ方法で「有効値」を解決し、モデルビルダーと一致させています:

```python
_main_val = int(_main_raw) if _main_raw else None
_aux_val = int(_aux_raw) if _aux_raw else LLM_CLIENT_DEFAULTS["aux_remote_max_tokens"]
```

覚えておくべき帰結:

- **`MAIN_LLM_MAX_TOKEN` が未設定**なら `None` に解決され、まっさらな起動でも常に拒否されます。
- **`AUXILIARY_LLM_MAX_TOKEN` が未設定**なら `LLM_CLIENT_DEFAULTS["aux_remote_max_tokens"] = 121072` に解決され、下限を下回るため同様に拒否されます。省略は近道ではありません。
- ローカル補助モード（`AUXILIARY_LLM_MODEL_LOCAL=true`）でもこれは変わりません。免除が存在しないからです（[制限](#%EF%B8%8F-制限と非目標)を参照）。

## 🧭 運用ガイド

### 不適合な設定を直す

1. リポジトリルートの `.env` を開くか、UI の システム設定 → 環境設定 タブを使います。
2. 両方のキーを `>= 131072` にします:

```bash
MAIN_LLM_MAX_TOKEN = 131072
AUXILIARY_LLM_MAX_TOKEN = 131072
```

3. 起動ゲートを再実行するためバックエンドを再起動します。同梱の `.env.example` では両方ともすでに `131072` です。

> ローカルの `.env` がまだ `100000` の場合、サーバーは `STARTUP ABORTED: MAIN_LLM_MAX_TOKEN = 100000 is below the minimum 131072 (128K); agent startup is blocked` とともに起動を拒否します。起動前に `>= 131072` へ引き上げてください。

### 症状、原因、対処

| 症状 | 原因 | 対処 |
| :------ | :---- | :-- |
| サーバーが即終了し、ログに `STARTUP ABORTED: ...` | キーが未設定か `131072` 未満 | 両キーを `>= 131072` にして再起動 |
| ログに `AUXILIARY_LLM_MAX_TOKEN = 121072 ...` | 補助キーが未設定で `121072` にフォールバック | `AUXILIARY_LLM_MAX_TOKEN` を明示設定 |
| 返信ではなく WebSocket エラーチャンク | 実行中の環境編集で値が下限を下回った | `.env` を直してターンを再試行 |
| 環境タブの保存で maxToken エラー | 値が非整数または `< 131072` | `>= 131072` の整数を入力 |
| 送信時に「モデル設定が最小値未満」toast | `GET /model-config` が `valid: false` を報告 | `.env` を直す。保存成功後にキャッシュは再取得される |

## 🧪 テスト

| スイート | マーカー | カバー |
| :---- | :----- | :----- |
| `tests/config/test_token_guard.py` | `unit` | しきい値は `131_072`。`None` と下限未満は例外。エラーはキー名を明示。下限以上は通過 |
| `tests/agent/core/test_built_agent_guard.py` | `unit` | `built_agent()` がメイン下限未満・メイン未設定・補助未設定フォールバックを拒否。有効な env では構築 |
| `tests/server/service/test_env_guard.py` | `unit` | `write_env_file()` が**書き込まずに**下限未満と非整数を拒否。有効値と非トークンキーは通過 |
| `tests/server/trigger/test_model_config.py` | `integration` | `GET /model-config` の形状と `valid`: 両方最小、メイン未満、補助未設定、メイン未設定 |
| `client/app/composables/__tests__/modelConfig.test.ts` | Vitest | 取得のキャッシュ破壊、一度だけのキャッシュ、invalidate 後の再取得、デフォルト無効フォールバック |
| `client/app/composables/__tests__/use-chat-stream.token.test.ts` | Vitest | `handleSend` は無効時のみ警告。有効時と取得失敗時は静か |
| `client/app/pages/home/components/__tests__/ConfigDialog.token.test.ts` | Vitest | バナーと保存前拒否。有効値は永続化しキャッシュを無効化 |

```bash
# Python (unit + integration)
uv run pytest tests/config/test_token_guard.py tests/agent/core/test_built_agent_guard.py \
  tests/server/service/test_env_guard.py tests/server/trigger/test_model_config.py -q

# Frontend
cd client && pnpm test:unit -- modelConfig use-chat-stream.token ConfigDialog.token
```

## ⚠️ 制限と非目標

- **免除スイッチはありません。** どちらのキーも下限から外れられません。環境フラグも、ロール別オーバーライドも、バイパスもありません。
- **ローカル補助モードは免除されません。** `AUXILIARY_LLM_MODEL_LOCAL=true` でも `>= 131072` を満たす必要があります。ローカルモデルの経路は `n_ctx=40960` にハードコードされているため、標準的なローカル構成はウィンドウが拡大するまで*拒否される見込み*です。
- **補助の未設定は罠であり近道ではありません。** `AUXILIARY_LLM_MAX_TOKEN` がない場合、解決されるフォールバックは `LLM_CLIENT_DEFAULTS["aux_remote_max_tokens"] = 121072` で下限未満なので、起動はどのみちブロックされます。
- **下限は 128K に固定です。** プロバイダー別しきい値も、上限も、実行時ホットリロードもありません。実行中の編集は次のグラフ構築（ゲート 2 が強制）にのみ効き、進行中のターンには決して効きません。
- **フロントエンドの toast は助言にすぎません。** 送信を妨げません。強制は完全にサーバー側です。
- **他のガードとは無関係。** `ToolGuardrails` や CJK トークン推定とは独立で、圧縮しきい値や `summarization` の調整には触れません。固定するのはコンテキストウィンドウの*最小値*であり、その窓の使い方ではありません。

## 📁 ファイルマップ

| 層 | ファイル | 役割 |
| :---- | :--- | :--- |
| しきい値 | `config/features/agent_side/token_guard.py` | `MIN_REQUIRED_MAX_TOKEN`、`TokenGuardError`、`assert_max_token_valid` |
| エクスポート | `config/features/__init__.py` | 3 シンボルを再エクスポート |
| 起動ゲート | `server/__main__.py:95-100` | CRITICAL ログの後 `SystemExit(1)` |
| 構築ゲート | `agent/core.py:123` | `built_agent()` が構築前に検証 |
| スポーンゲート | `agent/tools/subagent/spawn/core.py:815` | 子 LLM 構築時に検証 |
| 書き込みゲート | `server/service/env.py:157` | `write_env_file()` が書き込み前に拒否 |
| API | `server/trigger/http/model_config.py` | `GET /model-config` |
| フロントエンドキャッシュ | `client/app/composables/model-config.ts` | `fetchModelConfig`、`getModelConfigCached`、`invalidateModelConfigCache` |
| フロントエンド警告 | `client/app/composables/use-chat-stream.ts:435` | 送信時の非ブロッキング toast |
| フロントエンドダイアログ | `client/app/pages/home/components/ConfigDialog.vue` | バナー、保存前検証、キャッシュ無効化 |
| 既定値 | `.env.example:8,59` | 両キーとも `131072` |
