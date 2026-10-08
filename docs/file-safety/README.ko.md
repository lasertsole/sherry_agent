# 🛡️ 파일 쓰기 안전성

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

> 동시에 쓰는 자들——메인 Agent, 최대 8개의 서브 Agent, 그리고 같은 프로젝트 위의 두 번째 Sherry 프로세스——이 서로의 편집을 조용히 파괴하지 못하게 막는 방법: 원자적 쓰기, 이중 CAS, 프로세스 내 경로별 잠금, 프로세스 간 `flock`, `write_file`의 읽기 우선 라이선스, 그리고 잠금 아래 병합되는 선택적 격리 워크스페이스.

출처: `agent/tools/pub_base/atomic_write.py`, `agent/tools/pub_base/path_lock.py`, `agent/tools/pub_base/file_lock.py`, `agent/tools/pub_base/read_state.py`, `agent/tools/file_tools/write_file.py`, `agent/tools/file_tools/read_file.py`, `agent/tools/file_tools/patch_file.py`, `agent/tools/subagent/isolation/`, `agent/tools/subagent/announce/workspace_merge.py`. 이 문서의 모든 상수는 그 코드와 대조해 확인했습니다.

## 목차

- [개요](#-개요)
- [각 계층](#-각-계층)
  - [1. 원자적 쓰기](#1-원자적-쓰기)
  - [2. 이중 CAS](#2-이중-cas)
  - [3. 경로별 잠금](#3-경로별-잠금)
  - [4. 잔여 파일 청소](#4-잔여-파일-청소)
- [읽기 우선 라이선스](#-읽기-우선-라이선스)
- [격리 서브에이전트 워크스페이스 (git worktree)](#-격리-서브에이전트-워크스페이스-git-worktree)
- [대안과 실측](#%EF%B8%8F-대안과-실측)
- [리소스 파일과 인코딩](#%EF%B8%8F-리소스-파일과-인코딩)
- [경계](#-경계)
- [테스트](#-테스트)
- [파일 지도](#%EF%B8%8F-파일-지도)

## 🎯 개요

모든 파일 도구는 모든 서브 Agent와 같은 프로세스를 공유하고, 도구는 프로세스 수준 싱글턴입니다——즉 "한 경로에 두 명의 쓰는 자"는 예외가 아니라 일상입니다. 보호가 없으면 실패는 조용합니다: 나중에 쓴 쪽이 앞선 편집을 덮어쓰고 둘 다 성공을 보고합니다. 읽는 쪽은 반쯤 쓰인 파일을 붙잡습니다. 쓰는 도중의 크래시는 대상을 잘라냅니다.

여섯 가지 장치가 쓰기가 마주치는 순서대로 이에 답합니다:

1. `file_write_lock`——경로별 `threading.Lock`, 이어서 프로세스 간 `flock`.
2. `atomic_write_text_no_follow`——같은 디렉터리의 임시 파일, `fsync`, 모드 보존, `os.replace`.
3. `patch_file`의 이중 CAS——읽을 때의 지문과, `replace` 직전에 다시 주장되는 `expected_revision`.
4. `write_file`의 읽기 우선 라이선스——이미 있는 파일은 그것을 읽은 세션만 덮어쓸 수 있습니다(아래 해당 절).
5. `sweep_stale_temp_files`——`kill -9`가 남긴 것은 그 디렉터리에 대한 다음 쓰기가 청소합니다.
6. 격리 서브에이전트 워크스페이스——선택적 git worktree로, 변경은 잠금 아래 병합되고 충돌은 보고됩니다.

## 🧱 각 계층

### 1. 원자적 쓰기

`atomic_write_text_no_follow`(그 아래의 바이트 판 `atomic_write_bytes_no_follow` 포함)는 대상과 같은 디렉터리의 `.sherry-tmp-*.swp`에 쓰고, `fsync`하고, 대상의 모드를 복사한 뒤——스크립트의 실행 비트는 살아남아야 합니다——`os.replace`로 제자리에 놓습니다. 따라서 읽는 쪽은 옛 내용 아니면 새 내용만 보며, 찢어진 파일은 결코 보지 못하고, 쓰는 도중의 크래시도 대상을 자르지 못합니다.

의도된 두 가지 경계:

- 마지막 구성 요소가 심볼릭 링크면 거부합니다(`ELOOP`, `_open_no_follow` 경유). 그래서 이 헬퍼는 `pub/func/atomic_replace.py`를 재사용할 수 없습니다——그쪽은 의도적으로 링크를 따라갑니다.
- 파일 시스템이 rename을 거부하면 헬퍼는 제자리 쓰기로 강등됩니다——원자성보다 가용성, WARNING으로 기록합니다.

### 2. 이중 CAS

`patch_file`은 파일을 읽고, 흐릿하게 매칭한 뒤, 두 번 검증합니다: 읽을 때의 지문(`mtime_ns` + `size`, 내용 해시 면제가 있어 무의미한 `touch`는 충돌이 아닙니다)과, 원자적 쓰기가 `os.replace` 직전에 다시 주장하는 `expected_revision`(`mtime:<정수 밀리초>:size:<바이트>`). 다른 누구의 변경이든 덮어쓰이지 않고 "다시 읽고 재시도" 오류로 거부됩니다.

검증에서 쓰기까지의 창은 0이 아니며, 이 문서는 그렇게 주장하지 않습니다. 파일 시스템 수준 트랜잭션을 전제하지 않고 줄일 수 있는 가장 작은 간격까지 좁혀 두었습니다.

### 3. 경로별 잠금

`path_lock.py`는 참조 계수된 `threading.Lock` 레지스트리——해석된 경로마다 하나——를 유지하며 읽기·수정·쓰기 주기 전체를 감쌉니다. 그래서 이 프로세스 안의 두 Agent는 경쟁하지 않고 직렬화됩니다. `file_lock.py`가 이어서 프로세스 간 절반을 더합니다: `src/data/locks/` 아래 sidecar 파일에 대한 권고적 `flock`, 타임아웃은 `FileBusyError`(멈춤이 아니라 대처 가능한 오류)를 돌려줍니다.

사람을 놀라게 하는 규칙 하나: **잠금 파일은 결코 삭제하지 않습니다.** 잠금은 inode에 있으므로, sidecar를 `unlink`하면——낡아 보여도——다음 쓰는 자는 새 파일을 만들어 아무도 배제하지 않는 잠금을 잡습니다. 실측에서 두 보유자가 동시에 임계 구역에 들어갔습니다. 0바이트 sidecar는 불활성이고, 보유자가 죽으면 커널이 `flock`을 해제하므로 구현할 죽은 소유자 프로토콜은 없습니다.

`terminal`, `python_repl`, ast-grep 재작성은 서브프로세스에서 돌아 어떤 잠금도 잡지 않습니다: 그것들을 붙잡는 것은 CAS이며, 이 경계는 의도적입니다.

### 4. 잔여 파일 청소

임시 파일 생성과 replace 사이에 하드 킬된 경우에만 `.sherry-tmp-*.swp`가 남을 수 있습니다. 나머지 모든 실패 경로는 그것을 unlink합니다. 같은 디렉터리에 대한 다음 쓰기가 1시간을 넘긴 잔여를 청소합니다(`sweep_stale_temp_files`): 살아 있는 쓰는 자의 임시 파일은 몇 초밖에 살지 않으며, 나이 임계값이 바로 그것과 거리를 두기 위한 장치입니다. 청소는 결코 예외를 던지지 않습니다——디렉터리를 읽지 못하는 스캔이, 그것이 앞서는 쓰기를 실패시켜서는 안 됩니다.

## 📖 읽기 우선 라이선스

`write_file`은 눈감고 덮어쓰는 도구입니다: 대상을 읽지 않으므로 CAS에 실어 갈 revision이 없고, 이 세션이 한 번도 본 적 없는 내용을 파괴할 수 있었습니다. `read_state.py`가 빠져 있던 전제 조건을 공급합니다——프로세스 내의 `(세션, 해석된 경로) → revision` 매핑이며, 먹이는 두 사건뿐입니다:

- **완전한** `read_file`(첫 페이지, 잘리지 않음. 부분 읽기는 아무것도 허가하지 않습니다). revision은 연 디스크립터의 `fstat`에서 오므로, 읽는 중 바뀐 경로가 세션이 보지 못한 바이트를 허가할 수 없습니다.
- 세션 자신의 파일 전체 `write_file`. 그 내용은 세션이 쓴 것이기 때문입니다.

세션 자신의 `append`나 `patch_file`은 이미 가진 라이선스를 **전진**시킬 뿐, 결코 새로 만들지 않습니다——델타는 파일 전체에 대한 지식이 아닙니다. 라이선스는 `expected_revision`으로 원자적 쓰기에 실려 들어가므로:

- 세션이 한 번도 읽지 않은 기존 파일은 `read_file`/`patch_file` 힌트가 붙은 "먼저 읽으세요" 오류로 즉시 거부됩니다;
- 읽은 뒤 바뀐 파일은 동시 쓰는 자를 붙잡는 것과 같은 주장에 의해 거부됩니다;
- 아직 없는 경로는 `absent`로 허가되어, 다른 쓰는 자가 먼저 만든 경우 덮어쓰지 않고 거부됩니다;
- 디렉터리나 심볼릭 링크 대상은 게이트를 건너뜁니다——그 자신의 오류가 옳은 답이며, "먼저 읽으세요"는 무의미합니다.

레지스트리에는 상한이 있고(4096개, LRU) **영속화되지 않습니다**: 재시작하면 라이선스는 잊히고, 기존 파일에 대한 다음 덮어쓰기는 "먼저 읽으세요"를 돌려줍니다——대가는 다시 읽기 한 번이고, 잃는 편집은 결코 없습니다. 축출은 보호를 떨어뜨릴 뿐, 부여하지 않습니다.

## 🌱 격리 서브에이전트 워크스페이스 (git worktree)

`sessions_spawn(isolation=True)`는 자식에게 프로젝트의 **git worktree**를 줍니다(구현은 `agent/tools/subagent/isolation/`, 워크스페이스는 `src/data/isolated/` 아래, 자식의 cwd는 `<workspace>/tree`, 브랜치는 `sherry/<run8>`). 자식은 그곳에서 처음부터 끝까지 일하고——자기 도구, `terminal`, 테스트——실행이 종단에 이르면 완료 메시지 전달 전에 announce 흐름이 트리를 병합합니다(조용한 자식의 작업도 마찬가지로 병합됩니다).

베이스라인은 **더러운 작업 트리**입니다: `git stash create`가 사용자의 미커밋 변경을 트리를 자르는 revision으로 바꿉니다(stash 스택은 그대로). 따라서 자식은 사용자가 실제로 가진 내용을 봅니다——마지막 커밋이 아니라, 그리로 조용히 되돌아가는 일도 결코 없습니다. 체크아웃은 커밋된 내용만 나르므로, 나머지는 실행이 `SUBAGENT_ISOLATION` 두 표에 따라 물질화합니다(프로젝트의 `.worktreeinclude`가 덧붙일 수 있고, 경로 다음 줄에 `# wti=symlink|copy`):

- **심볼릭 링크**——공유 기계 상태: `src/`, `workspace/memory`, `workspace/sessions`, `cron_jobs.json`, `skills/auto`, `client/node_modules`. 모든 트리가 inode 하나를 공유하며, 그것이 SQLite의 `-wal`/`-shm` 일관성과 링크를 통해 잡은 잠금이 부모 쪽 기록자를 배제하는 성질(올바른 의미론)을 성립시킵니다;
- **복사**——두 트리가 공유해선 안 되는 가변 상태: `.omo`(계획과 진행);
- **미추적 파일**은 개별 복사됩니다: `git stash create`는 결코 그것들을 나르지 않으므로, 이 단계가 없으면 자식은 사용자가 방금 만든 파일을 보지 못합니다;
- 물질화되는 것은 git이 실제로 무시하는 경로뿐이며, 캐시(`node_modules`, `.git`, `.venv`, `__pycache__`, `dist`, `build` 등)는 복사도 병합도 되지 않습니다.

저장소가 아닌 프로젝트는 먼저 초기화됩니다: `git init`, 거부 패턴을 `.git/info/exclude`에(**`git add -A` 전에**——비밀·용량·저장소 상태는 그 베이스라인 커밋의 역사에 결코 들어가지 않으며 나중에 지워도 되돌릴 수 없습니다), 커밋 한 번, 그리고 복원 방법을 적은 `.sherry-isolation-repo` 마커(`rm -rf .git .sherry-isolation-repo`). sherry는 결코 push하지 않고, 리모트를 추가하지 않으며, 역사를 다시 쓰지 않습니다.

물질화 링크를 거치는 읽기는 격리 내부로 남습니다: `resolve_external_path`는 그 실행의 매니페스트가 기록한 경로만 면제하므로, 트리 안의 `src/…`는 HITL '외부 파일' 승인을 일으키지 않고 트리 밖을 가리키는 임의의 심볼릭 링크는 여전히 확인을 요구합니다.

병합은 다른 곳과 같은 규칙을 반대 방향으로 적용한 것이며, 부모 루트 단위 `flock` 아래에서 이루어집니다:

- 워크스페이스 매니페스트(`snapshot.json`)는 생성 시점의 일반 파일별 revision을 기록하고(구성은 트리에서, revision은 부모에서, mtime을 정렬해 건드리지 않은 파일이 변경으로 읽히지 않게 합니다) 베이스라인 revision과 브랜치도 기록합니다;
- 자식이 바꾼 파일은 부모가 아직 스냅샷 revision을 지니는 동안에만 적용됩니다——그렇지 않으면 **충돌**이고, 부모의 파일은 손대지 않은 채 남습니다;
- 새 파일은 빈 자리에만 만들어집니다; 삭제는 부모가 여전히 일치할 것을 요구합니다; 심볼릭 링크는 결코 관통 병합되지 않습니다(건너뛰고 보고합니다).

깨끗한 병합은 worktree를 등록 해제하고 그 브랜치를 지우고 워크스페이스를 제거합니다. 충돌이 있는 병합은 `<workspace>/tree`를 검사용으로 남기고 매니페스트를 소비합니다——같은 트리가 두 번 병합될 수 없도록. 부모가 읽는 완료 회신에는 보고가 실립니다(`applied`, `created`, `deleted`, 그리고 경로별 모든 충돌), 병합된 경로는 부모의 증거 원장에서 오래된 것으로 표시됩니다.

## ⚖️ 대안과 실측

**행 해시 편집(omo의 hashline)**은 구현하지 않았습니다: 파일 전체 CAS보다 정밀하지만——편집이 건드린 줄만 비교합니다——read 도구 자체의 출력 형식을 바꿔야 하고, 위의 4개 계층이 이미 그것이 겨냥한 조용한 손실 실패를 없앴습니다.

**rename 뒤의 디렉터리 `fsync`**는 켜지 않았습니다. 본 기기(f2fs) 실측: 쓰기 한 번이 디렉터리 `fsync` 포함 1.34 ms, 미포함 0.52 ms——쓰기 경로의 2.6배, 절대값으로 파일당 약 0.8 ms. 사는 것은 좁습니다: 파일 데이터는 이미 `fsync`되어 있어 정전이 찢어진 파일을 남기는 일은 없고——가능한 것은 편집이 이전 내용으로 조용히 되돌아가는 것뿐이며, 이는 주류 편집기도 마찬가지입니다. 프로세스 크래시에는 아무것도 필요 없습니다: 페이지 캐시가 남습니다. 어느 배치가 "확인된 쓰기는 정전을 견딘다"를 약속한다면, 플래그 뒤의 한 줄입니다.

## 🗂️ 리소스 파일과 인코딩

도구가 편집하는 것은 **텍스트**이며, 라이선스가 그 사실을 구조적 전제로 만들었습니다:
예전에는 PNG를 읽으면 치환 문자가 한 화면 모델에 전달되고 덮어쓰기 라이선스까지 붙어,
이미지가 깨진 문자로 대체될 수 있었습니다. 이제 `file_utils.sniff_text_encoding`이
먼저 BOM을, 다음으로 NUL 바이트 / UTF-8 디코드를 보고 판정합니다:

- **UTF-8, BOM 있는 UTF-8, UTF-16(LE/BE)** 은 정상적으로 읽고 쓰며, 라이선스가 코덱을
  함께 넘깁니다——`write_file`과 `patch_file`은 파일 **자신의** 인코딩으로 되쓰므로
  UTF-16 설정은 BOM과 바이트 순서를 유지하고, 빅엔디언 파일은 빅엔디언으로 남습니다.
- **그 밖은 리소스 파일**입니다: `read_file`은 크기와 `terminal` 힌트만 돌려주고
  **라이선스를 전혀 주지 않으며**, `patch_file`은 거부하고, `write_file`은 라이선스를
  보기도 전에 거부합니다. 이것이 이미지나 아카이브가 텍스트로 대체되지 않게 하는
  장치입니다: 바이너리는 라이선스를 결코 갖지 않으므로 어떤 것도 텍스트로 쓸 수 없습니다.
- **BOM 없는 구형 코덱(GBK/GB18030)은 추측하지 않고 거부**합니다——잘못 추측해 다른
  코덱으로 다시 쓰는 것은 대처 가능한 거부보다 나쁩니다. 탈출구는 `terminal`(cp / python).
- **`append`는 UTF-8 전용**입니다: UTF-16 파일에 덧붙이면 UTF-8 바이트를 쓰거나 파일
  중간에 두 번째 BOM을 심게 되므로 "읽고 전체를 다시 쓰라"는 답을 돌려줍니다.
- 격리 워크스페이스의 병합은 처음부터 끝까지 바이트 단위이므로, 텍스트 도구가 편집하지
  않는 리소스 파일도 정확히 병합됩니다.

## 🚧 경계

- 프로세스 간 잠금은 권고적입니다: 외부 편집기는 잡지 않습니다. 거기서의 답은 CAS 계층입니다.
- `terminal`, `python_repl`, ast-grep 재작성은 잠금과 라이선스를 완전히 우회합니다(서브프로세스). 셸 명령으로 파일을 쓰는 Agent는 이 보장 밖입니다.
- 네트워크 파일 시스템: `flock` 의미론은 NFS에서 신뢰할 수 없습니다. 로컬 디스크가 대상입니다.
- 하드 링크는 설계상 끊깁니다: `os.replace`가 inode를 바꾸므로, 옛 내용을 가리키는 하드 링크는 옛 내용을 유지합니다.

## 🧪 테스트

`tests/agent/tools/file_tools/`가 쓰기 경로를 고정합니다: `test_atomic_write.py`(원자성, 심볼릭 링크 거부, 두 CAS 계층, 잔여 청소), `test_file_write_concurrency.py`(병렬 패치, 찢어진 읽기 부재), `test_file_lock_cross_process.py`(실제 프로세스 둘, `kill -9` 해제), `test_read_before_write.py`(라이선스 매트릭스). `tests/agent/tools/subagent/test_workspace_isolation.py`는 더러운 베이스라인, auto-init 거부 목록, 물질화(링크·복사·미추적 파일), 외부 경로 면제, 병합 CAS, 충돌, 심볼릭 링크 건너뛰기, 부모 루트 단위 직렬화를 고정합니다.

## 🗺️ 파일 지도

| 경로 | 역할 |
| --- | --- |
| `agent/tools/pub_base/atomic_write.py` | 원자적 쓰기, revision 식별자, 잔여 청소 |
| `agent/tools/pub_base/path_lock.py` | 프로세스 내 경로별 잠금 레지스트리 |
| `agent/tools/pub_base/file_lock.py` | `flock` sidecar, `file_write_lock`, `FileBusyError` |
| `agent/tools/pub_base/read_state.py` | 읽기 우선 라이선스 레지스트리 |
| `agent/tools/subagent/isolation/worktree.py` | 더러운 베이스라인, auto-init과 거부 목록, worktree 수명주기 |
| `agent/tools/subagent/isolation/materialize.py` | 무시·미추적 경로 물질화 |
| `agent/tools/subagent/isolation/tree.py` | 워크스페이스 worktree, 매니페스트, 폐기 |
| `agent/tools/subagent/isolation/merge.py` | 잠금·CAS 검증 병합 |
| `agent/tools/subagent/announce/workspace_merge.py` | 병합 훅 + 완료 회신 속 보고 |
