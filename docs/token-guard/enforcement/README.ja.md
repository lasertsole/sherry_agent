# 🛡️ 適用、エンドポイント、フロントエンド

[English](README.md) · [中文](README.zh.md) · [한국어](README.ko.md) · **日本語**

> [Token Guard](../README.ja.md) の一部：128K 下限を強制する 4 つのゲート、読み取り専用の `GET /model-config` エンドポイント、警告のみでブロックしないフロントエンドの挙動。

---

## 🚧 適用ポイント

4 つのゲート、1 つの共有述語。表が契約で、詳細は後述します。

| # | 場所 | 失敗時の挙動 | ブロック |
| :- | :--- | :--------------- | :-------- |
| 1 | `server/__main__.py` 起動ゲート | `logger.critical("STARTUP ABORTED: ...")` の後 `SystemExit(1)` | プロセス終了、何も提供しない |
| 2 | `agent/core.py::built_agent()` | `TokenGuardError` が呼び出し元へ伝播（WebSocket エラーチャンク） | そのターンのグラフは構築されない |
| 3 | `agent/tools/subagent/spawn/core.py` スポーン | `TokenGuardError` がスポーン呼び出しから外へ伝播 | 子エージェントは決して構築されない |
| 4 | `server/service/env.py::write_env_file()` | ファイルに触れる前に `ValueError` | 保存拒否、`.env` は不変 |

### 1. サーバー起動ゲート

`server/__main__.py`（ゲートは 72 行目から）は agent-core の作業より前にフェイルファストするので、誤設定の `.env` が不完全なエージェントを提供することは決してありません:

```python
from config.features import LLM_CLIENT_DEFAULTS, assert_max_token_valid

_main_raw = os.getenv("MAIN_LLM_MAX_TOKEN", "").strip()
_aux_raw = os.getenv("AUXILIARY_LLM_MAX_TOKEN", "").strip()

# an unset AUXILIARY_LLM_MAX_TOKEN falls back to
# LLM_CLIENT_DEFAULTS["aux_remote_max_tokens"] = 121072 (< 128K), so leaving
# it unset blocks startup too. Local mode (AUXILIARY_LLM_MODEL_LOCAL=true)
# is NOT exempt.
_main_val = int(_main_raw) if _main_raw else None
_aux_val = int(_aux_raw) if _aux_raw else LLM_CLIENT_DEFAULTS["aux_remote_max_tokens"]

try:
    assert_max_token_valid("MAIN_LLM_MAX_TOKEN", _main_val)
    assert_max_token_valid("AUXILIARY_LLM_MAX_TOKEN", _aux_val)
except Exception as e:
    logger.critical("STARTUP ABORTED: {}", e)
    raise SystemExit(1) from e
```

不正な値ではまさにこの行が出て、プロセスは起動しません:

```text
CRITICAL  STARTUP ABORTED: MAIN_LLM_MAX_TOKEN = 100000 is below the minimum 131072 (128K); agent startup is blocked
```

### 2. グラフ構築ゲート

`agent/core.py`（ゲートは 117 行目）は実行時の第二防衛線です。サーバーが有効な `.env` で起動していても、後からの編集でどちらかの値が 128K を下回れば、グラフの構築を拒否します:

```python
_main_raw = os.getenv("MAIN_LLM_MAX_TOKEN", "").strip()
_aux_raw = os.getenv("AUXILIARY_LLM_MAX_TOKEN", "").strip()
_main_val = int(_main_raw) if _main_raw else None
_aux_val = int(_aux_raw) if _aux_raw else LLM_CLIENT_DEFAULTS["aux_remote_max_tokens"]
assert_max_token_valid("MAIN_LLM_MAX_TOKEN", _main_val)
assert_max_token_valid("AUXILIARY_LLM_MAX_TOKEN", _aux_val)
```

`TokenGuardError` は呼び出し元へ伝播し、`server/service/messages.py` がそれを WebSocket エラーチャンクとしてクライアントに提示します。

### 3. サブエージェントスポーンゲート

`agent/tools/subagent/spawn/core.py`（ゲートは 762 行目）は子 LLM を検証します。スポーンは `built_agent()` を迂回するため、下限は暗黙に継承されないからです:

```python
# Subagent spawn bypasses built_agent(), so child LLM construction validates
# the same 128K MAX_TOKEN floor here instead of inheriting it implicitly.
...
assert_max_token_valid("MAIN_LLM_MAX_TOKEN", _main_val)
assert_max_token_valid("AUXILIARY_LLM_MAX_TOKEN", _aux_val)
```

### 4. 環境書き込みゲート

`server/service/env.py`（`write_env_file()`、ゲートは 154 行目）は、UI から 128K 未満の値を永続化できないようにします。非整数または下限未満の値は、`.env` に触れる前に `ValueError` を投げるので、バックアップも書き込みも汚れません:

```python
if key in TOKEN_KEYS:
    try:
        num = int(value)
    except ValueError:
        raise ValueError(f"{key} must be an integer, got: {value}")
    if num < MIN_REQUIRED_MAX_TOKEN:
        raise ValueError(
            f"{key} must be >= {MIN_REQUIRED_MAX_TOKEN} (128K); got {num}. "
            f"Refusing to save."
        )
```

HTTP ハンドラーはそれを `{"success": False}` に変換してクライアントへ返し、ディスク上の `.env` はバイト単位で同一のままです。

## 🌐 エンドポイント: GET /model-config

`server/trigger/http/model_config.py` は、フロントエンドが参照する読み取り専用エンドポイントを登録します。解決済みの両値、しきい値、判定を報告します:

```json
{
  "main_max_token": 131072,
  "aux_max_token": 131072,
  "min_required": 131072,
  "valid": true
}
```

`valid` が `true` になるのは、3 つの条件がすべて成立するときだけです:

```python
"valid": (
    main_val is not None
    and main_val >= MIN_REQUIRED_MAX_TOKEN
    and aux_val >= MIN_REQUIRED_MAX_TOKEN
),
```

| 状況 | `main_max_token` | `aux_max_token` | `valid` |
| :-------- | :--------------- | :-------------- | :------ |
| 両方 `131072` | `131072` | `131072` | `true` |
| メインが下限未満 | 例 `65536` | `131072` | `false` |
| 補助が未設定 | `131072` | `121072`（フォールバック） | `false` |
| メインが未設定 | `null` | `131072` | `false` |

このリソースに書き込みエンドポイントはありません（環境の書き込みは別の `/env` ルートを通ります）。`GET /model-config` は状態を決して変更しません。

## 💻 フロントエンドの挙動

クライアントは早めに警告しますが、ユーザーを決してブロックしません。強制するのはあくまでバックエンドです。

### キャッシュ付き取得

`client/app/composables/model-config.ts` がキャッシュを所有します:

- `fetchModelConfig()` はキャッシュ破壊用の `_ts` クエリパラメータを付けて `GET /model-config` を呼び、空の本文をデフォルト無効オブジェクトに置き換えます。
- `getModelConfigCached()` は一度だけ取得してモジュール全体でキャッシュします。取得失敗時はデフォルト無効オブジェクト（`valid: false`）をキャッシュするので、バックエンドの一時的な不調が送信経路を壊すことはありません。
- `invalidateModelConfigCache()` はキャッシュを消します。`ConfigDialog` が環境保存の成功後に呼ぶので、次の `getModelConfigCached()` は再取得します。

```ts
function invalidModelConfig(): ModelConfig {
  return { main_max_token: null, aux_max_token: null, min_required: 131072, valid: false };
}
```

### 送信時の非ブロッキング警告

`client/app/composables/use-chat-stream.ts`（`handleSend()`、toast は 433 行目）はキャッシュ設定を読み、`valid` が `false` のとき 6 秒の警告 toast を出します。送信は中止しません:

```ts
const cfg = await getModelConfigCached();
if (!cfg.valid) {
  toastWarn(t('chat.tokenGuard.title'), t('chat.tokenGuard.detail'), 6000);
}
```

設定が有効なとき、または取得に失敗したときは toast は静かなままです。送信はそのまま進み、バックエンドが構築を拒否すれば WebSocket エラーチャンクを返します。

### 環境タブのバナーと保存前検証

`client/app/pages/home/components/ConfigDialog.vue`:

- 環境タブが `*_MAX_TOKEN` キーを一つでも公開すると（`hasMaxTokenKeys`、344 行目）、琥珀色のバナーが `config.env.maxTokenHint`（211 行目）を描画します。
- 保存時に `persistEnvChanges()` が両方のトークンキーを事前検証します（379 行目から 389 行目）。非整数または `131072` 未満の値は `config.env.maxTokenError` を設定して `false` を返すので、PUT は決して送られません:

```ts
const TOKEN_KEYS = ['MAIN_LLM_MAX_TOKEN', 'AUXILIARY_LLM_MAX_TOKEN'];
const MIN_TOKEN = 131072;
for (const [key, value] of Object.entries(changes)) {
  if (TOKEN_KEYS.includes(key)) {
    const num = parseInt(value, 10);
    if (Number.isNaN(num) || num < MIN_TOKEN) {
      envLoadError.value = t('config.env.maxTokenError', { key });
      return false;
    }
  }
}
```

- 書き込み成功後（756 行目）に `invalidateModelConfigCache()` が走り、toast ガードが最新値を読み直します。

### i18n キー

| キー | 場所 | 用途 |
| :-- | :---- | :------ |
| `chat.tokenGuard.title` | `client/app/i18n/locales/{en,zh,ja,ko}.json` | toast のタイトル |
| `chat.tokenGuard.detail` | 同上のロケールファイル | toast の本文（システム設定 → 環境設定 を指す） |
| `config.env.maxTokenHint` | `ConfigDialog.vue` の `<i18n>` ブロック | 琥珀色バナーの文言 |
| `config.env.maxTokenError` | `ConfigDialog.vue` の `<i18n>` ブロック | 保存前拒否メッセージ。`{key}` が問題の変数名に展開される |
