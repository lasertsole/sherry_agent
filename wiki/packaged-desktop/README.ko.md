# 패키지 데스크톱 시작 신뢰성

[English](README.md) · [中文](README.zh.md) · **한국어** · [日本語](README.ja.md)

> Tauri 통합 설치 프로그램이 Windows 첫 실행을 버티도록 만드는 방법: 동봉 설정 템플릿은 PyInstaller 동결 트리에서 찾고, QQ SDK는 런타임 pip 대신 백엔드에 포함하며, 트레이 종료에는 윈도우 권한을 추가하고, 클라이언트 입력 오류는 500이 아닌 400을 반환합니다.

## 최초 설정 시드

셸은 `--sherry-home <app-data>`를 전달합니다. `config/_home.resolve_code_root()`는 **동봉 템플릿**에 대해 `sys._MEIPASS`(onedir의 `_internal`)를 우선하고, `ROOT_DIR`은 기존처럼 프로젝트/스킬 루트로 남습니다. `sherry.jsonc`, `.models`, `.network`, `.env`는 최초 import 시 이 템플릿 루트에서 시드됩니다. 템플릿이 없으면 조용히 건너뛰지 않고 warning을 남기므로 패키지 구성 이상을 로그에서 바로 진단할 수 있습니다.

단일 파일 템플릿에는 추가 패키징 규칙이 있습니다: PyInstaller `--add-data`의 DEST는 **디렉터리**입니다. `.models.example`을 DEST로 쓰면 같은 이름의 디렉터리가 생기고 최초 실행 시더가 디렉터리를 보고 건너뛰어 모델/앱 설정 탭이 비어 보입니다. 빌드는 모든 파일 항목을 번들 루트(`.`)로 보내고, `check_seed_files`는 시드 템플릿이 일반 파일이 아닌 동결 트리를 거부하며, `tests/packaging/test_build_backend.py`가 둘 다 고정합니다.


## 동결 환경과 QQ

동결 백엔드에는 가상 환경이 없고 자체 인터프리터에 패키지를 설치할 수 없습니다. `channels/deps.install_requirements()`와 `channels/registry._ensure_deps()`는 `sys.frozen`이 참일 때 즉시 반환합니다. QQ SDK(`botpy`, 패키지명 `qq-botpy`)는 프로젝트 의존성에 추가되어 `packaging/build_backend.py`가 수집하므로 플러그인이 정상적으로 import합니다. 누락된 빌드에서는 INFO 한 줄만 남기고 채널을 비활성화하며, 시작할 때마다 `uv pip install`을 재시도하지 않습니다.

## 트레이 종료 동작

`CloseToTrayDialog`는 Tauri IPC로 `getCurrentWindow().hide()`를 호출합니다. `core:default`는 읽기 전용 윈도우 조회만 허용하므로 `client/src-tauri/capabilities/default.json`에 `core:window:allow-hide`를 명시적으로 추가합니다. 이것이 없으면 대화상자가 종료를 확인해도 창은 그대로 남습니다. capability는 Rust 셸에 컴파일되므로 설치 프로그램을 다시 빌드해야 합니다.

## HTTP 클라이언트 입력 오류

패키지 라우트 프로브에서 다섯 개 handler가 의도된 클라이언트 오류를 일반 500으로 흘려보내는 것을 발견했습니다. 미디어 필수/안전하지 않은 파라미터, 히스토리 페이지의 누락/비정수 필드, pending interrupt 세션 누락, `DELETE /sessions`의 JSON body/session 누락, memory entry id 비정수입니다. 이제 HTTP 경계에서 공통 `bad_request`/`not_found`(400/404)를 반환합니다.

## 검증

초점 테스트는 동결 템플릿 루트 해석과 네 개 시드 파일, 동결 환경 설치 단락, QQ SDK 수집, 트레이 capability, 다섯 개 400 응답을 다룹니다. 최초 실행의 엔드투엔드 확인은 새로운 Windows 사용자 프로필에서 설치 프로그램을 다시 빌드해 수행해야 합니다.

출처: `config/_home.py`, `config/path.py`, `config/sherry_settings.py`, `config/models_settings.py`, `config/network_settings.py`, `channels/deps.py`, `channels/registry.py`, `plugins/channels/qq/core.py`, `packaging/build_backend.py`, `server/trigger/http/`, `client/src-tauri/capabilities/default.json`.
