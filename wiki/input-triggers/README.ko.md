# 컴포저 입력 트리거

[English](README.md) · [中文](README.zh.md) · **한국어** · [日本語](README.ja.md)

> 채팅 클라이언트의 일부: `$`는 스킬, `/`는 사용자 정의/내장 명령을 선택하며, `+` 버튼이 같은 선택기(그리고 왼쪽 사이드바 파일 트리)를 엽니다. 컴포저는 contenteditable + 원자 chip 그대로이며 에디터 프레임워크를 쓰지 않습니다.

## 트리거 목록

| 트리거 | 소스 | 선택 결과 |
|---|---|---|
| `$` | `GET /skills`(active이고 메인에서 보이는 스킬) | 원자 `$skillName` chip 삽입, `extractTextForSend`는 `$skillName` 전송 |
| `/` | `GET /commands?session_id=…` 사용자 정의 명령 + 기존 명령 레지스트리 | 사용자 정의는 확장된 본문을 가진 원자 chip, 내장은 즉시 실행 |
| `@` | 기존 파일 mention(드래그, 파일 트리 "채팅에 추가") | 기존 `@path` chip 유지 |
| `+` | 컴포저 툴바 | 스킬 선택 / 명령 선택 / 왼쪽 사이드바 파일 트리 표시 |

## `$` 스킬

`use-skills-list.ts`는 `GET /skills`를 캐시하고 `active !== false`이며 `scope`가 `subagent_only`가 아닌 스킬만 남깁니다. `use-input-trigger.ts`는 입력 시작 또는 공백 뒤의 `$`(중국어 `¥` / `￥` 별칭 포함)만 감지하고 접두사·부분 문자열·설명으로 필터링합니다. 선택하면 입력된 토큰을 `skill-mention-chip`으로 바꾸고 표시는 `$name`, 전송 텍스트도 `$name`입니다.

시스템 프롬프트의 안정적인 `## Skill References` 블록에 따라 사용자 메시지에 `$name`이 있으면 Agent는 `skill_view`로 해당 스킬을 먼저 읽습니다.

## `/` 명령

사용자 정의 명령은 Markdown 파일입니다: 사용자 범위는 `APP_HOME/commands/`, 프로젝트 범위는 `<project>/.sherry/commands/`(같은 이름이면 프로젝트 우선). `server/service/custom_commands_service.py`가 frontmatter의 `description` / `argument-hint`와 본문을 읽고 `GET /commands?session_id=…`가 목록을 반환합니다. 내장 명령은 `use-command-registry.ts`(Ctrl+K와 동일)에서 옵니다.

`argument-hint`가 있는 사용자 정의 명령은 선택기에서 인자 입력란을 엽니다. 선택 후 `command-mention-chip`의 `data-command-body`에 `$ARGUMENTS`를 치환한 본문을 담고, `extractTextForSend`는 표시 이름이 아니라 본문을 전송합니다. 내장 명령은 레지스트리 handler를 실행하고 chip을 넣지 않습니다.

## `+` 메뉴와 `@` 파일 mention

`+` 버튼은 숨은 `$` / `/`의 발견 가능한 입구입니다. 세 행이 스킬 선택기, 명령 선택기, 왼쪽 사이드바 프로젝트 파일 트리를 엽니다. 파일 참조는 기존 `@path` chip(드래그 또는 트리의 "채팅에 추가")을 그대로 씁니다.

## 사용자 정의 명령 형식

```markdown
---
description: Review the current change
argument-hint: [area]
---
Review the current git diff. Focus on: $ARGUMENTS
```

명령 이름은 파일 이름(중첩은 `:`로 연결), `description`은 필수, `$ARGUMENTS`는 단순 텍스트 치환입니다. 셸 확장이나 `!command`는 지원하지 않습니다.

## 안전과 테스트

트리거는 단어 경계에서만 동작하고 붙여넣은 `<pre>` / `<code>` 안에서는 발동하지 않으며 `contenteditable=false` chip 안에서 다시 발동하지 않습니다. Backspace로 query를 지우고 트리거 문자를 지우면 패널이 닫힙니다. 테스트: `client/app/composables/__tests__/use-input-trigger.test.ts`, 컴포저 통합 테스트, `tests/server/service/test_custom_commands_service.py`.
