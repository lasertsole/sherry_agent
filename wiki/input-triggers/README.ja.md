# コンポーザー入力トリガー

[English](README.md) · [中文](README.zh.md) · [한국어](README.ko.md) · **日本語**

> チャットクライアントの一部：`$` はスキル、`/` はカスタム/組み込みコマンドを選択し、`+` ボタンが同じピッカー（と左サイドバーのファイルツリー）を開きます。コンポーザーは contenteditable + 原子 chip のままで、エディタフレームワークは使いません。

## トリガー一覧

| トリガー | ソース | 選択結果 |
|---|---|---|
| `$` | `GET /skills`（active かつメインから見えるスキル） | 原子 `$skillName` chip を挿入し、`extractTextForSend` は `$skillName` を送る |
| `/` | `GET /commands?session_id=…` のカスタムコマンド + 既存のコマンドレジストリ | カスタムは展開済み本文を持つ原子 chip、組み込みは即実行 |
| `@` | 既存のファイル mention（ドラッグ、ファイルツリーの「チャットに追加」） | 既存の `@path` chip は変更なし |
| `+` | コンポーザーのツールバー | スキル選択 / コマンド選択 / 左サイドバーのファイルツリー表示 |

## `$` スキル

`use-skills-list.ts` は `GET /skills` をキャッシュし、`active !== false` かつ `scope` が `subagent_only` でないスキルだけを残します。`use-input-trigger.ts` は入力の先頭または空白後の `$`（中国語の `¥` / `￥` も同義）だけを検出し、前方一致・部分一致・説明で絞り込みます。選択すると入力済みトークンを `skill-mention-chip` に置換し、表示は `$name`、送信テキストも `$name` です。

システムプロンプトの安定した `## Skill References` ブロックにより、ユーザー入力に `$name` があれば Agent は `skill_view` でそのスキルを読み込んでから作業します。

## `/` コマンド

カスタムコマンドは Markdown ファイルです：ユーザー範囲は `APP_HOME/commands/`、プロジェクト範囲は `<project>/.sherry/commands/`（同名はプロジェクトが優先）。`server/service/custom_commands_service.py` が frontmatter の `description` / `argument-hint` と本文を読み、`GET /commands?session_id=…` が一覧を返します。組み込みコマンドは `use-command-registry.ts`（Ctrl+K と同じ）から来ます。

`argument-hint` を持つカスタムコマンドは、ピッカー内で引数入力欄を開きます。選択後は `command-mention-chip` の `data-command-body` に `$ARGUMENTS` を置換した本文を保持し、`extractTextForSend` は表示名ではなく本文を送ります。組み込みはレジストリの handler を実行し、chip を挿入しません。

## `+` メニューと `@` ファイル mention

`+` ボタンは隠れた `$` / `/` の見つけやすい入口です。3 行でスキルピッカー、コマンドピッカー、左サイドバーのプロジェクトファイルツリーを開きます。ファイル参照は既存の `@path` chip（ドラッグまたはツリーの「チャットに追加」）のままです。

## カスタムコマンド形式

```markdown
---
description: Review the current change
argument-hint: [area]
---
Review the current git diff. Focus on: $ARGUMENTS
```

コマンド名はファイル名（ネストは `:` で連結）、`description` は必須、`$ARGUMENTS` は単純なテキスト置換です。シェル展開や `!command` はサポートしません。

## 安全性とテスト

トリガーは単語境界でのみ有効で、貼り付けた `<pre>` / `<code>` 内では発火せず、`contenteditable=false` の chip 内でも再発火しません。Backspace で query を削除し、トリガー文字を消すとパネルが閉じます。テスト：`client/app/composables/__tests__/use-input-trigger.test.ts`、コンポーザーの統合テスト、`tests/server/service/test_custom_commands_service.py`。
