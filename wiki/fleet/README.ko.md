# 🏢 베어메탈 Fleet: 테넌트당 Sherry 프로세스 1개

[**English**](README.md) · [中文](README.zh.md) · **한국어** · [日本語](README.ja.md)

> Fleet 배포는 테넌트마다 일반 Sherry 백엔드를 하나씩 실행하고 그 앞에 단일 Ingress를 둡니다. 각 cell은 자체 `SHERRY_HOME`, workspace, SQLite 저장소를 가지며, Ingress가 브라우저 로그인을 담당하고 인증된 트래픽만 전달합니다. Docker는 필요 없고, cell은 공용 인터페이스를 수신하지 않습니다.

**정보 출처:** `fleet/`, `packaging/MULTI_TENANT.md`, `tests/fleet/`.

## 🎯 무엇인가

배포는 세 계층으로 구성됩니다. 브라우저는 Ingress(`python -m fleet serve`, 기본 포트 8081)에만 연결합니다. Ingress는 사용자를 인증하고 JWT claims에서 테넌트를 찾은 뒤 HTTP와 WebSocket 트래픽을 해당 테넌트의 cell로 역프록시합니다. cell은 일반 `python -m server` 프로세스이며 20100-20299 범위의 포트에서 `127.0.0.1`에 바인딩됩니다. Fleet manager가 cell 전용 gateway token을 넣어 detached로 시작합니다. 테넌트 상태는 하나의 디렉터리 트리에 있습니다: `SHERRY_HOME`, `workspace/`, `sherry.jsonc`, `logs/`.

## 🏗️ 토폴로지

```text
browser ──HTTP(S)──▶ Ingress :8081 ──loopback──▶ cell A  python -m server :20100
                             │                      SHERRY_HOME=<fleet_root>/a
                             ├──loopback──▶ cell B  python -m server :20101
                             │                      SHERRY_HOME=<fleet_root>/b
                             └──loopback──▶ shared daemons :9012 :9013 :9014 :9015
```

Ingress와 모든 cell은 같은 OS 사용자로 실행됩니다. 이것은 신뢰할 수 있는 테넌트 계층입니다. 경계는 프로세스 상태, 포트, 라우팅이며 커널 샌드박스가 아닙니다.

## 🚪 Ingress: 인증과 브라우저 계약

Ingress는 모든 `/auth/*` 경로를 소유하며 `/auth/token`을 절대 프록시하지 않습니다. 따라서 cell이 부팅할 때마다 생성하는 gateway token이 브라우저로 유출되지 않습니다. 로그인은 두 개의 HttpOnly cookie(`sherry_ingress_access`, `sherry_ingress_refresh`)를 설정합니다. access token은 `tenants`(기본값이 먼저), `tenant_id`, `role`을 담은 단명 HS256 JWT이고, refresh token은 사용할 때마다 회전하며 서버 측에서 폐기할 수 있습니다.

공유 SPA가 보는 계약은 단일 테넌트 cell과 같습니다. `/auth/status`는 추가로 `username_required`를 반환하고, `/auth/login`은 `{username, password}`를 받아 `expires_in`을 반환합니다. `/auth/me`는 유효한 refresh cookie로 만료된 access cookie를 치유하고, `/auth/account`는 비밀번호를 변경합니다(로그인 활성화/비활성화는 관리자만 가능). 안전하지 않은 메서드 요청은 Origin과 `Sec-Fetch-Site` CSRF 검사를 통과해야 합니다. 동일한 `(client, username)`이 300초 안에 5회 실패하면 `429`를 반환합니다.

`SHERRY_INGRESS_TLS_CERT`와 `SHERRY_INGRESS_TLS_KEY`를 설정하면 Ingress가 TLS를 종료하고 두 cookie에 `Secure`를 표시합니다. `SHERRY_INGRESS_ALLOWED_ORIGINS`는 역프록시 origin을 추가합니다. 정적 자산은 공개되며, `Accept: text/html`이 있는 모든 GET은 `200.html` 또는 `index.html`을 받습니다. 따라서 `/login`과 깊은 경로는 세션이 없어도 로드됩니다. API 호출은 계속 인증이 필요합니다.

## 👥 사용자, 신뢰 도메인, 셀 전환

셀은 사용자 계정이 아니라 신뢰 도메인입니다. 여러 계정이 같은 `tenant_id`를 가리켜 해당 셀의 세션, 메모리, 스킬, 프로젝트 디렉터리를 공유할 수 있습니다. 자격 증명은 계정별로 Ingress 데이터베이스에 남으며, 한 계정의 비밀번호를 바꿔도 다른 계정은 영향을 받지 않습니다. 한 계정이 여러 셀에 속할 수도 있으며, 이 관계는 다대다 `user_tenants` 테이블에 저장됩니다(기존 `users.tenant_id`는 기본값으로 유지). `init()`은 `tenant_id`가 `UNIQUE`였던 이전 `users` 테이블을 한 번 재구축하고 멤버십을 채웁니다.

로그인은 계정의 기본 멤버십을 선택하고 인증 cookie 옆에 HttpOnly `sherry_tenant` cookie를 설정합니다. `GET /auth/tenants`는 각 멤버십의 `role`, `is_default`, `cell_status`를 반환하고, `POST /auth/tenant`는 요청한 `tenant_id`를 실시간 테이블로 검증한 뒤 cookie를 다시 씁니다. 프록시와 WebSocket 라우터는 요청마다 이 cookie를 읽고 멤버십을 재확인하므로 관리자가 셀 접근을 취소하면 즉시 반영됩니다. 오래된 cookie는 계정의 기본값으로 대체됩니다. SPA의 `TenantSwitcher`는 계정에 둘 이상의 셀이 있을 때만 표시되며, 전환 후 페이지를 새로 고칩니다. 세션 캐시, 메모리, 소켓이 모두 이전 셀에 속하기 때문입니다.

멤버십이 없는 계정은 로그인할 수 없습니다(`no_tenant`). 접근 관리에는 `user-tenant-add`, `user-tenants`, `user-tenant-remove`를 사용하며, 첫 멤버십이 owner이자 기본값입니다. `rm`은 셀을 삭제하지만 멤버십은 남기므로 먼저 취소하거나 재할당하십시오.

## 🧩 Cell 라이프사이클

`python -m fleet create --tenant alice`는 `<fleet_root>/alice/{workspace,logs,sherry.jsonc}`를 만들고 20100-20299에서 첫 번째 빈 포트를 할당합니다. 포트 선택과 레지스트리 쓰기는 프로세스 내부 잠금과 fleet 루트의 `flock`으로 직렬화되므로 두 CLI 프로세스가 같은 포트를 등록할 수 없습니다. `start`는 `SHERRY_HOME`, `SHERRY_PROJECT_DIR`, `API_PORT`, `SHERRY_GATEWAY_TOKEN`, `PUBLIC_BASE_URL`, `SHERRY_DAEMON_HOME`, `SHERRY_DAEMON_CLIENT_ONLY`를 주입하고 서버를 detached로 시작한 뒤 예산이 끝날 때까지 `/health`를 폴링합니다.

`stop`은 환경 변수가 여전히 해당 cell의 `SHERRY_HOME`을 가리키는 PID에만 신호를 보냅니다. 재사용되었거나 외부의 PID는 건드리지 않습니다. `create`는 `--seed-config`도 지원하여 호스트의 `.models`와 `.network` 파일을 복사합니다(자격 증명이 들어 있으므로 기본은 꺼져 있음).

## 🛡️ 감독과 리소스 제한

| 증상 | Supervisor의 대응 |
| :--- | :--- |
| 기록된 PID가 사라졌거나 `SHERRY_HOME`이 더 이상 일치하지 않음 | 오래된 자식 프로세스를 회수하고 대체 프로세스를 시작 |
| 프로세스는 살아 있지만 `/health`가 연속 3회 실패 | `stop` 후 `start` |
| 중지된 cell이 회수되지 않은 zombie를 남김 | 대체 프로세스가 포트를 바인딩하기 전에 `waitpid`로 회수 |
| `logs/cell.log`가 `SHERRY_FLEET_LOG_MAX_BYTES`를 초과 | `cell.log.1`로 copy-truncate. 실행 중인 O_APPEND 디스크립터에 안전 |
| cell이 `SHERRY_FLEET_CELL_MEMORY_MB` 또는 `SHERRY_FLEET_CELL_NOFILE`을 초과 | exec 래퍼가 `ulimit`을 적용했으며, CPU 할당량은 cgroup 또는 systemd의 몫 |

Supervisor는 Ingress 안에서 실행되며 `SHERRY_FLEET_SUPERVISE_INTERVAL_S`초마다 각 running cell을 검사합니다. `SHERRY_FLEET_SUPERVISE=0`은 이 루프를 끄고 외부 운영에 맡깁니다.

## 💾 백업과 복구

`python -m fleet backup --tenant alice --out <dir>`는 cell 트리를 복사하면서 `logs/`, `-wal`, `-shm`을 건너뛰고, 각 `.db`를 `sqlite3.Connection.backup()`으로 스냅샷합니다. 따라서 SQLite 파일은 트랜잭션 일관성을 유지합니다. cell이 실행 중일 때 데이터베이스가 아닌 파일은 best-effort입니다.

`restore`는 cell을 중지하고 백업을 staging에 풀고, 현재 트리를 `<tenant>.pre-restore`로 이름을 바꾼 뒤 staging을 교체합니다. `python -m fleet backup-db --out <dir>`는 중앙 `fleet.db`(사용자, refresh token, cell, 프로세스 행)를 백업합니다.

## 🤝 공유 모델 데몬

`python -m fleet daemons-start`는 `<fleet_root>/daemons` 아래에서 각 로컬 모델 데몬(embed, reranker, local chat, vision)의 유일한 사본을 소유합니다. cell은 `SHERRY_DAEMON_CLIENT_ONLY=1`로 실행되어 기록된 pid에 연결하고, 두 번째 사본을 시작하지 않으며 atexit kill도 등록하지 않습니다. `SHERRY_DAEMON_HOME`은 모든 cell을 공유 pid 디렉터리로 향하게 합니다.

Local chat과 vision에는 `SHERRY_FLEET_AUXILIARY_MODEL_PATH`, `SHERRY_FLEET_VISION_MODEL_PATH`, `SHERRY_FLEET_VISION_MMPROJ_PATH`도 필요합니다. 이들이 없으면 명령은 절대 로드할 수 없는 데몬을 시작하지 않고 누락된 변수를 보고합니다. 데몬이 내려가 있으면 클라이언트는 프로세스 내 로더로 폴백하므로 기능은 유지되지만 cell별 메모리 공유는 사라집니다.

## 🖥️ CLI

```bash
python -m fleet serve
python -m fleet user-add --username alice --password '...' --tenant alice
python -m fleet user-tenant-add --username alice --tenant team-b --role member
python -m fleet user-tenants --username alice
python -m fleet user-tenant-remove --username alice --tenant team-b
python -m fleet create --tenant alice
python -m fleet start --tenant alice
python -m fleet stop --tenant alice
python -m fleet list
python -m fleet backup --tenant alice --out /srv/backups/alice
python -m fleet restore --tenant alice --from /srv/backups/alice
python -m fleet backup-db --out /srv/backups/fleet
python -m fleet daemons-start
python -m fleet daemons-stop
python -m fleet daemons-status
```

## 🔐 보안 경계

| 경계 | 배포가 보장하는 것 |
| :--- | :--- |
| 브라우저 자격 증명 | HttpOnly, SameSite=Lax cookie. `/auth/token`은 Ingress에서 404 |
| Cell 자격 증명 | cell마다 `SHERRY_GATEWAY_TOKEN` 하나. 브라우저가 보낸 token과 ticket은 제거됨 |
| Cell 네트워크 | cell은 `127.0.0.1`에만 바인딩하고 Ingress는 loopback으로 접근 |
| 테넌트 파일 | `SHERRY_HOME`이 데이터 트리를 분리하지만 모든 cell이 하나의 OS 사용자를 공유. 적대적 테넌트에는 P3 컨테이너 계층이 필요 |

- 같은 tenant의 계정은 해당 셀의 세션과 메모리를 공유합니다. 사용자별 격리에는 별도 테넌트가 필요합니다.

## 🧪 테스트

`tests/fleet/`는 저장소, Ingress 인증, 프록시, WebSocket, TLS, manager 라이프사이클, 공유 데몬, 백업/복구를 다룹니다. `test_cell_e2e.py`는 manager를 통해 실제 `python -m server` cell을 시작하고 `/health`, PID 동일성, 로그인, HTTP 프록시, 정상 SIGTERM 종료를 검증합니다.

`pytest tests/fleet`과 저장소의 split gate가 회귀 명령입니다. 클라이언트 인증에는 자체 store, 로그인 페이지, 라우트 가드, 계정 패널 테스트군이 있고, 백엔드 전체 gate는 unit, integration, regression을 별도 프로세스로 실행합니다.

## 📌 운영 요구사항

- Ingress는 systemd 또는 supervisord로 감독하십시오. 내부 supervisor는 cell만 관리하고 자기 자신은 관리하지 않습니다.
- SPA는 `VITE_API_BACK_URL=same-origin`으로 동일 출처 빌드하고, `PUBLIC_BASE_URL`을 브라우저에서 접근 가능한 Ingress origin으로 설정하십시오.
- 안정적인 `SHERRY_INGRESS_JWT_SECRET`을 설정하십시오. 부팅마다 무작위로 생성하면 재시작 시 모든 cookie가 무효화됩니다.
- TLS는 Ingress 또는 신뢰할 수 있는 역프록시에서 종료하고, 추가 origin을 `SHERRY_INGRESS_ALLOWED_ORIGINS`에 나열하십시오.
- manager가 담당하지 않는 제한에는 cgroup 또는 systemd CPU 할당량과 외부 백업/logrotate 타이머를 추가하십시오.
- 테넌트마다 프로젝트 디렉터리를 바인딩하십시오. 설정하지 않으면 두 테넌트가 `ROOT_DIR/.sherry/coordination.db`를 공유합니다.
- 모든 계정에 최소 하나의 멤버십을 부여하십시오. 멤버십이 없으면 로그인은 `no_tenant`를 반환합니다.
