# 🏢 Fleet 배포: 테넌트당 Sherry cell 1개

[**English**](README.md) · [中文](README.zh.md) · **한국어** · [日本語](README.ja.md)

> Fleet 배포는 테넌트마다 Sherry 백엔드를 하나씩 실행하고 그 앞에 단일 Ingress를 둡니다. 각 cell은 자체 `SHERRY_HOME`, workspace, SQLite 저장소를 가지며, Ingress가 브라우저 로그인을 담당하고 인증된 트래픽만 전달합니다. 같은 인터페이스에 두 가지 백엔드가 있습니다. 신뢰할 수 있는 테넌트를 위한 베어메탈 **process** 계층과, 신뢰할 수 없는 테넌트를 위한 **container** 계층(Docker 또는 Podman)이며 후자는 OS 수준의 파일 시스템·네트워크·cgroup 격리를 제공합니다. cell은 공용 인터페이스를 수신하지 않습니다.

**정보 출처:** `fleet/`, `packaging/MULTI_TENANT.md`, `tests/fleet/`.

## 🎯 무엇인가

배포는 세 계층으로 구성됩니다. 브라우저는 Ingress(`python -m fleet serve`, 기본 포트 8081)에만 연결합니다. Ingress는 사용자를 인증하고 JWT claims에서 테넌트를 찾은 뒤 HTTP와 WebSocket 트래픽을 해당 테넌트의 cell로 역프록시합니다. cell은 일반 `python -m server` 프로세스이거나 같은 서버 이미지를 실행하는 컨테이너이며, 20100-20299 범위의 포트에서 `127.0.0.1`에 바인딩됩니다. Fleet manager가 cell 전용 gateway token을 넣어 시작합니다. 테넌트 상태는 하나의 디렉터리 트리(`SHERRY_HOME`, `workspace/`, `sherry.jsonc`, `logs/`)에 있으며 두 백엔드 모두 같은 트리를 마운트하거나 가리키므로 테넌트를 두 계층 사이에서 옮길 수 있습니다.

## 🏗️ 토폴로지

```text
browser ──HTTP(S)──▶ Ingress :8081 ──loopback──▶ cell A  process  :20100
                             │                      SHERRY_HOME=<fleet_root>/a
                             ├──loopback──▶ cell B  container :20101
                             │                      /app/data ← <fleet_root>/b
                             └──loopback──▶ shared daemons :9012 :9013 :9014 :9015
```

Ingress와 모든 cell은 같은 OS 사용자로 실행됩니다. process 계층은 신뢰 테넌트용이며 경계는 프로세스 상태, 포트, 라우팅이고 커널 샌드박스가 아닙니다. container 계층은 별도의 마운트 네임스페이스, 전용 bridge 네트워크, 엄격한 CPU·메모리·pids 상한을 추가합니다.

## 🚪 Ingress: 인증과 브라우저 계약

Ingress는 모든 `/auth/*` 라우트를 소유하며 `/auth/token`은 역프록시하지 않습니다. 따라서 cell이 부팅마다 생성하는 gateway token이 브라우저로 새지 않습니다. 로그인은 HttpOnly cookie 두 개(`sherry_ingress_access`, `sherry_ingress_refresh`)를 설정합니다. access token은 수명이 짧은 HS256 JWT로 `tenants`(기본값이 먼저), `tenant_id`, `role`을 담습니다. refresh token은 사용할 때마다 회전하며 서버 측에서 폐기할 수 있습니다.

공유 SPA는 단일 테넌트 cell과 같은 계약을 보지만 한 가지가 다릅니다. Ingress 로그인은 "사용자 이름 + 비밀번호"이므로(아래 다중 사용자 절 참고) `/auth/status`가 `username_required: true`도 반환합니다. `/auth/me`는 유효한 refresh cookie로 만료된 access cookie를 복구하고, 안전하지 않은 메서드는 Origin과 `Sec-Fetch-Site` CSRF 검사를 통과해야 합니다.

`SHERRY_INGRESS_TLS_CERT`와 `SHERRY_INGRESS_TLS_KEY`를 설정하면 Ingress가 TLS를 종료하고 두 cookie를 `Secure`로 표시합니다. `SHERRY_INGRESS_ALLOWED_ORIGINS`는 역프록시 origin을 추가합니다. 정적 자산은 공개되며 `Accept: text/html` GET은 `200.html` 또는 `index.html`을 받으므로 `/login`과 깊은 라우트는 세션 없이도 로드됩니다. API 호출은 계속 제한됩니다.

## 👥 다중 사용자 기능: 계정, cell, 전환

Ingress만 "사람"을 알며, cell은 사용자 이름을 보지 않습니다. 계정은 **관리자가 미리 만들어 두는** 것이고(셀프 가입 없음) Ingress 데이터베이스(`users`와 다대다 `user_tenants`)에 있습니다. SPA는 Ingress에서 항상 로그인 화면을 보여 주며(`/auth/status`가 `auth_required: true`, `username_required: true`, `has_account: true`를 반환), `POST /auth/login`은 `{username, password}`를 받아 `expires_in`을 돌려줍니다. 실패는 `(클라이언트 주소, 사용자 이름)` 단위로 제한되어 300초에 5회면 `429`를 반환하고, 알 수 없는 사용자 이름도 더미 해시 검증을 수행하므로 응답 시간으로 계정 존재를 추측할 수 없습니다. 멤버십이 없는 계정은 `403 no_tenant`로 거부됩니다.

| 개념 | 저장 위치 | 의미 |
| :--- | :--- | :--- |
| 계정 | Ingress `users` | 사용자 이름 + scrypt 해시. 사람이 보유한 유일한 자격 증명입니다. 비밀번호 변경은 셀프서비스(`PUT /auth/account`)이고, 로그인 활성화/비활성화와 계정 삭제는 관리자 작업이며 `managed_by_admin`을 반환합니다. |
| `role` | `users.role` / `user_tenants.role` | 기록되고 반환되지만(`GET /auth/tenants`, `/auth/me`) **권한 판정에는 쓰이지 않습니다**. 인가는 멤버십 기반이므로 현재 `owner`와 `member`는 같은 cell에 동일하게 접근합니다. 이 라벨은 메타데이터로 취급하십시오. |
| cell(신뢰 도메인) | 하나의 `tenant_id` | 하나의 workspace, 하나의 메모리 저장소, 한 세트의 세션과 모델 설정. 내부의 모든 것은 모든 멤버가 공유합니다. |
| 멤버십 | `user_tenants(user_id, tenant_id, role, is_default)` | 한 계정을 한 cell에 인가합니다. 다대다이므로 한 계정이 여러 cell에, 한 cell이 여러 계정에 쓰일 수 있습니다. |

**공유되는 것과 계정별인 것.** 같은 cell의 멤버는 세션, 대화 기록, 메모리(FACTS / MEMORY / USER), 스킬, 프로젝트 디렉터리, 모델 설정을 공유합니다. 그것이 신뢰 도메인의 목적입니다. 자격 증명, refresh token 회전, 선택한 cell은 계정/브라우저 단위입니다. 두 계정은 서로의 비밀번호를 볼 수 없지만, 대화는 cell에 속하므로 서로 볼 수 있습니다.

**전환과 회수.** 로그인은 계정의 기본 멤버십을 선택하고 인증 cookie 옆에 HttpOnly `sherry_tenant` cookie를 설정합니다. `GET /auth/tenants`는 `role`, `is_default`, 실시간 `cell_status`를 포함한 모든 멤버십을 반환하고, `POST /auth/tenant`는 요청된 `tenant_id`를 실제 테이블로 검증한 뒤 cookie를 다시 씁니다. 프록시와 WebSocket 라우터는 요청마다 멤버십을 재확인하므로 `user-tenant-remove`는 즉시 반영됩니다. 오래된 cookie는 계정의 기본 cell로 폴백합니다. SPA의 `TenantSwitcher`는 cell이 둘 이상일 때만 나타나며 전환 후 페이지를 다시 로드합니다(캐시된 세션, 메모리, 소켓이 모두 이전 cell의 것이기 때문입니다).

**설정 빠른 안내.**

```bash
python -m fleet create --tenant team-a                     # OS 격리가 필요하면 --backend container
python -m fleet user-add --username alice --password '...' --tenant team-a
python -m fleet user-tenant-add --username bob --tenant team-a --role member
python -m fleet user-tenants --username alice
python -m fleet user-tenant-remove --username bob --tenant team-a
```

첫 멤버십이 owner이자 기본값입니다. `rm`은 cell만 삭제하고 멤버십은 남기므로 먼저 회수하거나 재배정하십시오.

**사용자 간 격리.** cell **내부**에는 격리가 없습니다. cell이 격리 단위입니다. 서로의 세션을 보여 주지 않아야 한다면 각자의 cell을 주고, 그 cell이 신뢰할 수 없는 테넌트의 것이라면 container 계층(파일 시스템·네트워크·cgroup 경계)을 사용하십시오. 컨테이너 cell은 자체 로그인을 구조상 끄며(`SHERRY_AUTH_ENABLED=false`와 강제 해제, container 절 참고), Ingress 트래픽은 loopback이 아니라 bridge 게이트웨이에서 도착하므로 실제로 의미 있는 로그인은 Ingress의 로그인입니다.

## 🧩 Cell 라이프사이클

`python -m fleet create --tenant alice`는 `<fleet_root>/alice/{workspace,logs,sherry.jsonc}`를 만들고 20100-20299에서 첫 번째 빈 포트를 할당합니다. 포트 선택과 등록 쓰기는 프로세스 잠금과 fleet 루트 `flock`으로 직렬화되므로 두 CLI 프로세스가 같은 포트를 등록할 수 없습니다. `--backend process`(기본값)는 `runtime=process`를 기록하고, `--backend container`는 `runtime=container`와 이미지, 컨테이너 이름을 기록합니다. 이후 명령은 이 열을 읽으므로 백엔드를 다시 지정할 필요가 없습니다.

`start`는 `SHERRY_HOME`, `SHERRY_PROJECT_DIR`, `API_PORT`, `SHERRY_GATEWAY_TOKEN`, `PUBLIC_BASE_URL`, `SHERRY_DAEMON_HOME`, `SHERRY_DAEMON_CLIENT_ONLY`를 주입하고 서버를 detached로 시작한 뒤 예산이 끝날 때까지 `/health`를 폴링합니다.

`stop`은 환경 변수가 여전히 해당 cell의 `SHERRY_HOME`을 가리키는 PID에만 신호를 보냅니다. 재사용되었거나 외부의 PID는 건드리지 않습니다. `create`는 `--seed-config`도 받아 호스트의 `.models`와 `.network`를 복사합니다(이 파일들은 자격 증명을 담으므로 기본적으로 꺼져 있습니다).

## 📦 Container 계층(OS 수준 격리)

`python -m fleet create --tenant alice --backend container --image sherry-agent:dev`는 같은 데이터 트리와 같은 Ingress 라우트를 유지하지만 cell은 자체 컨테이너에서 실행됩니다.

```text
--name sherry-cell-alice        --init          --restart unless-stopped
--cap-drop=ALL                  --security-opt no-new-privileges
--memory 4g --cpus 2            --pids-limit 1024
--network sherry-cell-alice-net --volume <fleet_root>/alice:/app/data
-p 127.0.0.1:20100:8080         --add-host host.docker.internal:host-gateway
--label sherry.fleet.tenant=alice --label sherry.fleet.attempt=<id>
```

0600 env 파일이 `SHERRY_HOME=/app/data`, `SHERRY_PROJECT_DIR=/app/data/workspace`, gateway token, `API_PORT`, `SHERRY_AUTH_ENABLED=false`, `PUBLIC_BASE_URL`, `LOCAL_DAEMON_HOST=host.docker.internal`을 담고, `create`가 읽은 직후 삭제됩니다. 이미지는 저장소의 Dockerfile 그대로이므로 `docker build` 또는 `podman build`가 유일한 이미지 단계입니다.

상태 확인은 `/health`와 함께 attempt 라벨을 대조합니다. 포트에 응답하더라도 `sherry.fleet.attempt`가 이번 것이 아니면 잔여 컨테이너로 간주하므로, 엔진이 재시작한 이전 컨테이너가 새 컨테이너를 가장할 수 없습니다.

**두 가지 네트워크 모드.** `SHERRY_FLEET_CONTAINER_NETWORK=publish`(Linux와 macOS의 기본값)는 테넌트마다 bridge 네트워크를 만들고 `127.0.0.1:<port>`를 게시합니다. 컨테이너끼리는 서로 도달할 수 없습니다. `host`는 런타임의 네트워크 네임스페이스를 공유하고 cell이 할당된 포트를 직접 수신합니다. WSL2 커널은 nftables를 제공하지 않아 netavark가 게시 규칙을 적용할 수 없으므로 이것이 WSL2 폴백입니다(`auto`는 Windows 호스트에서 `host`, 그 외에서는 `publish`를 고릅니다). host 모드는 네트워크 격리를 이식성과 맞바꿉니다. 커널이 허용하는 한 `publish`를 유지하십시오.

**cell 자체 로그인은 구조상 꺼져 있습니다.** Ingress에서 오는 트래픽은 loopback이 아니라 bridge 게이트웨이에서 도착하므로, cell이 자기 로그인을 강제하면 모든 프록시 요청에 `401`을 반환합니다. 새 cell은 `SHERRY_AUTH_ENABLED=false`로 `auth_settings.enabled`를 0으로 시드합니다. **백업에서 복구한** cell은 활성화된 로그인을 다시 가져올 수 있습니다. manager는 `/auth/status`로 감지해 `<data_dir>/src/data/auth.db`의 행을 직접 끄고 cell의 2초 상태 캐시가 만료되기를 기다립니다. 그래도 활성 상태이면 `create`/`start`가 분명히 실패하며, 아무도 접근할 수 없는 cell을 남기지 않습니다.

**업그레이드와 감사.** `python -m fleet upgrade --tenant alice --image <tag>`는 컨테이너를 멈추고 새 이미지로 같은 데이터 트리에 다시 만듭니다. 새 이미지의 상태 확인이 실패하면 이전 이미지로 롤백합니다. `python -m fleet doctor [--tenant alice]`는 모든 컨테이너 cell을 검사합니다. 컨테이너 존재와 실행, `cap-drop`이 엔진의 핵심 케이퍼빌리티 집합을 제거했는지(podman은 확장 목록, docker는 `ALL` 보고), `no-new-privileges` 설정, 메모리·CPU·pids 상한이 0이 아닌지, fleet 라벨이 테넌트와 일치하는지, 게시 바인딩이 `127.0.0.1`뿐이거나 `host` 모드인지, 테넌트 네트워크에 컨테이너가 하나뿐인지, 데이터 디렉터리가 `0700`(POSIX)인지, `/health`가 응답하는지. 하나라도 실패하면 명령은 1로 종료합니다.

## 🛡️ 감독과 리소스 제한

| 증상 | Supervisor 대응 |
| :--- | :--- |
| 기록된 PID가 사라졌거나 `SHERRY_HOME`이 더 이상 일치하지 않음 | 오래된 자식을 회수하고 대체 프로세스를 시작 |
| 프로세스는 살아 있지만 `/health`가 연속 3회 실패 | `stop` 후 `start` |
| 중지된 cell이 회수되지 않은 좀비를 남김 | 대체 프로세스가 포트를 바인딩하기 전에 `waitpid`로 회수 |
| `logs/cell.log`가 `SHERRY_FLEET_LOG_MAX_BYTES`를 초과 | 실행 중인 O_APPEND 디스크립터에 안전한 copy-truncate로 `cell.log.1` 생성 |
| cell이 `SHERRY_FLEET_CELL_MEMORY_MB` 또는 `SHERRY_FLEET_CELL_NOFILE` 초과 | exec 래퍼가 `ulimit`을 적용했으며, CPU 할당량은 여전히 cgroup 또는 systemd의 몫 |
| 컨테이너 cell이 exited 상태이거나 사라짐 | `start`가 다시 만들고 시작(데이터 트리는 그대로) |
| 컨테이너 cell은 실행 중이지만 `/health`가 연속 3회 실패 | `stop` 후 `start` |

Supervisor는 Ingress 안에서 실행되며 `SHERRY_FLEET_SUPERVISE_INTERVAL_S`초마다 실행 중인 cell을 검사합니다. `SHERRY_FLEET_SUPERVISE=0`은 이 루프를 끄고 운영자 관리에 맡깁니다.

## 💾 백업과 복구

`python -m fleet backup --tenant alice --out <dir>`는 `logs/`, `-wal`, `-shm`을 건너뛰고 cell 트리를 복사하며, 모든 `.db`를 `sqlite3.Connection.backup()`으로 스냅샷하므로 SQLite 파일은 트랜잭션 일관성을 가집니다. cell이 실행 중일 때 데이터베이스가 아닌 파일은 최선 노력 복사입니다. container 계층은 같은 호스트 측 트리를 사용하므로 백업과 복구 방식은 그대로입니다.

`restore`는 cell을 멈추고 백업을 스테이징한 뒤 현재 트리를 `<tenant>.pre-restore`로 이름을 바꾸고 스테이징한 사본을 넣습니다. `python -m fleet backup-db --out <dir>`는 중앙 `fleet.db`(사용자, refresh token, cell, 프로세스 행)를 백업합니다.

## 🤝 공유 모델 daemon

`python -m fleet daemons-start`는 각 로컬 모델 daemon(embed, reranker, 로컬 채팅, 비전)의 유일한 사본을 `<fleet_root>/daemons` 아래에 소유합니다. cell은 `SHERRY_DAEMON_CLIENT_ONLY=1`로 실행되어 기록된 pid에 연결할 뿐, 두 번째 사본을 시작하지 않고 atexit kill도 등록하지 않습니다. `SHERRY_DAEMON_HOME`이 모든 cell을 공유 pid 디렉터리로 향하게 합니다.

로컬 채팅과 비전에는 `SHERRY_FLEET_AUXILIARY_MODEL_PATH`, `SHERRY_FLEET_VISION_MODEL_PATH`, `SHERRY_FLEET_VISION_MMPROJ_PATH`도 필요합니다. 이것이 없으면 명령은 절대 로드할 수 없는 daemon을 시작하는 대신 누락된 변수를 보고합니다. daemon이 내려가 있으면 클라이언트는 프로세스 내 로더로 폴백하므로 기능은 유지되지만 cell별 메모리 공유는 사라집니다.

컨테이너 cell은 `host.docker.internal`을 통해 호스트 daemon에 도달하므로 daemon은 런타임이 라우팅할 수 있는 주소에서 수신해야 합니다(호스트 측 `LOCAL_DAEMON_HOST=0.0.0.0`). VM 기반 런타임의 host 네트워크 모드에서는 컨테이너가 보는 "호스트"가 VM이며 데스크톱이 아닙니다. 로컬 모델은 process cell 또는 Linux 호스트에 두십시오.

## 🖥️ CLI

```bash
python -m fleet serve
python -m fleet user-add --username alice --password '...' --tenant alice
python -m fleet user-tenant-add --username alice --tenant team-b --role member
python -m fleet user-tenants --username alice
python -m fleet user-tenant-remove --username alice --tenant team-b
python -m fleet create --tenant alice                                  # process 백엔드
python -m fleet create --tenant bob --backend container --image sherry-agent:dev
python -m fleet start --tenant alice
python -m fleet stop --tenant alice
python -m fleet upgrade --tenant bob --image sherry-agent:next          # container 전용
python -m fleet doctor --tenant bob                                    # container 감사
python -m fleet list
python -m fleet backup --tenant alice --out /srv/backups/alice
python -m fleet restore --tenant alice --from /srv/backups/alice
python -m fleet backup-db --out /srv/backups/fleet
python -m fleet daemons-start
python -m fleet daemons-stop
python -m fleet daemons-status
```

## 🔐 보안 경계

| 경계 | 배포가 보장하는 내용 |
| :--- | :--- |
| 브라우저 자격 증명 | HttpOnly, SameSite=Lax cookie. `/auth/token`은 Ingress에서 404 |
| Cell 자격 증명 | cell마다 하나의 `SHERRY_GATEWAY_TOKEN`. 브라우저가 보낸 token과 ticket 값은 제거됨 |
| Cell 네트워크 | cell은 `127.0.0.1`에만 바인딩하고 Ingress는 loopback으로 접근 |
| 테넌트 파일(process) | `SHERRY_HOME`이 데이터 트리를 분리하지만 모든 cell이 하나의 OS 사용자를 공유. 악의적인 테넌트에는 container 계층이 필요 |
| 테넌트 파일(container) | 테넌트 트리 위의 전용 마운트 네임스페이스, `cap-drop=ALL`, `no-new-privileges`, 엄격한 메모리·CPU·pids 상한. `terminal`과 `python_repl`은 다른 테넌트의 데이터를 읽을 수 없음 |
| 컨테이너 네트워크 | `publish` 모드는 테넌트마다 전용 bridge와 loopback 전용 게시 포트를 제공하며 컨테이너끼리 도달 불가 |
| 리소스 고갈 | 컨테이너 OOM이나 pids 폭주는 자체 cgroup 안에서 끝나며 Ingress와 다른 cell에 영향을 주지 않음 |

- 같은 테넌트의 여러 계정은 해당 cell의 세션과 메모리를 공유합니다. 사용자별 격리에는 별도 테넌트가 필요합니다.
- 컨테이너 cell은 컨테이너 내부에서 자기 테넌트를 신뢰합니다. 경계는 테넌트 대 테넌트이며 사용자 대 사용자가 아닙니다.

## 🧪 테스트

`tests/fleet/`는 저장소, Ingress 인증, 프록시, WebSocket, TLS, manager 라이프사이클, 공유 데몬, 백업/복구를 다룹니다. `test_cell_e2e.py`는 manager를 통해 실제 `python -m server` cell을 시작하고 `/health`, PID 동일성, 로그인, HTTP 프록시, 정상 SIGTERM 종료를 검증합니다.

container 백엔드는 `test_container_manager.py`(기록형 가짜 런타임: 명령 형태, env 파일, 두 네트워크 모드, 상태 실패 시 정리, 업그레이드 롤백, doctor 지적, 감독 루프, 복구된 로그인 해제)와 실제 엔진에 `create`를 요청하는 옵트인 스모크 테스트 `test_container_runtime_e2e.py`를 추가합니다.

```bash
SHERRY_FLEET_CONTAINER_E2E=1 uv run --no-sync pytest tests/fleet/test_container_runtime_e2e.py
```

`pytest tests/fleet`과 저장소의 split gate가 회귀 명령입니다. 클라이언트 인증에는 자체 store, 로그인 페이지, 라우트 가드, 계정 패널 테스트군이 있고, 백엔드 전체 gate는 unit, integration, regression을 별도 프로세스로 실행합니다.

## 📌 운영 요구사항

- Ingress는 systemd 또는 supervisord로 감독하십시오. 내부 supervisor는 cell만 관리하고 자기 자신은 관리하지 않습니다.
- SPA는 `VITE_API_BACK_URL=same-origin`으로 동일 출처 빌드하고, `PUBLIC_BASE_URL`을 브라우저에서 접근 가능한 Ingress origin으로 설정하십시오.
- 안정적인 `SHERRY_INGRESS_JWT_SECRET`을 설정하십시오. 부팅마다 무작위로 생성하면 재시작 시 모든 cookie가 무효화됩니다.
- TLS는 Ingress 또는 신뢰할 수 있는 역프록시에서 종료하고, 추가 origin을 `SHERRY_INGRESS_ALLOWED_ORIGINS`에 나열하십시오.
- manager가 담당하지 않는 제한에는 cgroup 또는 systemd CPU 할당량과 외부 백업/logrotate 타이머를 추가하십시오.
- 테넌트마다 프로젝트 디렉터리를 바인딩하십시오. 설정하지 않으면 두 테넌트가 `ROOT_DIR/.sherry/coordination.db`를 공유합니다.
- 모든 계정에 최소 하나의 멤버십을 부여하십시오. 멤버십이 없으면 로그인은 `no_tenant`를 반환합니다.
- container 계층에는 Ingress 호스트의 Docker 또는 Podman 바이너리(`SHERRY_FLEET_CONTAINER_RUNTIME`), 빌드된 이미지(`SHERRY_FLEET_IMAGE`, `SHERRY_FLEET_CONTAINER_PULL`), 그리고 이미지와 별도로 유휴 cell당 약 1 GB 메모리가 필요합니다. 호스트의 실제 용량에 맞춰 `SHERRY_FLEET_CONTAINER_MEMORY` / `_CPUS` / `_PIDS`를 설정하십시오.
- POSIX 호스트에서는 컨테이너가 fleet 사용자의 uid로 실행되므로 `0700` 데이터 트리를 chown 없이 쓸 수 있습니다. 다른 런타임 uid를 쓰려면 이미지에서 `--user`를 지원하거나 rootless subuid 매핑을 구성하십시오.
