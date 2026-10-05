# sesametok-backend

macOS에 설치·로그인된 **카카오톡 데스크톱 앱**을 데이터 소스이자 전송 채널로 사용해,
**조회 / 수신 / 발신**을 로컬 API로 제공하는 **단일 Python 프로세스**입니다.

이 프로젝트는 **백엔드만** 담당합니다. 화면(대화 UI)을 그리지 않고, Slack·Telegram·TUI 같은
인터페이스도 구현하지 않습니다. 그런 프로젝트들이 이 백엔드의 로컬 API를 소비합니다.

- 확정된 결정과 그 근거: [`DECISIONS.md`](DECISIONS.md)

---

## 무엇을 하는가

| 기능 | 내용 |
|---|---|
| 조회 | 방 목록, 대화 조회(기간·개수·커서 페이지네이션), 메시지 단건 |
| 수신 | 주기적 폴링 → 변경 감지 → 로컬 암호화 아카이브 → 재개 가능한 이벤트 스트림(SSE·webhook) |
| 발신 | 지정 방에 텍스트/이미지 전송, 초안 남기기, 사전 확인(dry-run) |

## 무엇을 하지 않는가

- Docker·컨테이너·VM·원격 서버. macOS 네이티브 단일 프로세스입니다.
- TUI/GUI 대화 화면, Slack/Telegram/Discord 어댑터(후속 인터페이스 프로젝트).
- 검색·RAG·임베딩.
- **미디어·이모티콘 수집(다운로드·로컬 복사·별도 폴링)**. 지원하지 않는 메시지 타입은
  본문을 `[지원하지 않는 메시지 타입입니다. 원본 메시지를 확인해주세요.]`로 치환해 저장하고,
  원본 타입은 `original_message_type`에 남깁니다(`is_placeholder=true`).
- **외부 네트워크 통신**. API는 loopback 전용이고 webhook 대상도 loopback만 허용합니다.

---

## 요구사항

- macOS (Apple Silicon), 카카오톡 macOS 앱 설치·로그인·실행
- Python 3.13 (개발·검증 환경: 3.13.3). `sqlcipher3==0.6.3`이 cp313 arm64 wheel을 제공합니다.
- 전체 디스크 접근 권한(읽기), 손쉬운 사용 권한(발신)

---

## 설치

```bash
# 개발용 (저장소 안에 venv)
python3 -m venv .venv
./.venv/bin/pip install -e '.[dev]'

# 운영용 (TCC 안정성을 위해 경로를 고정)
python3 -m venv ~/.local/share/sesametok-backend/venv
~/.local/share/sesametok-backend/venv/bin/pip install .
```

`bin/sesametok-backend`는 venv를 활성화하지 않고 실행하는 얇은 래퍼입니다.
`SESAMETOK_VENV`로 venv 경로를 지정할 수 있고, 기본은 저장소의 `./.venv`입니다.

---

## 실행

```bash
sesametok-backend foreground     # 개발용: 현재 터미널에서 실행
sesametok-backend status         # 실행/권한/폴링 상태 (--json 가능)
sesametok-backend start          # launchd로 기동 (터미널을 닫아도 유지, 멱등)
sesametok-backend stop
sesametok-backend restart
sesametok-backend logs -n 100
sesametok-backend token [--copy]         # 인터페이스용 API 토큰
sesametok-backend archive-key [--force]  # 아카이브 키 백업용 출력
sesametok-backend permissions [--accessibility]
sesametok-backend install        # 로그인 시 자동 시작 등록
sesametok-backend uninstall
```

### 첫 실행에서 반드시 할 일: 아카이브 키 백업

첫 기동 시 계정 식별은 (1) 환경변수 → (2) 로컬 캐시 → (3) 외부 스킬 캐시 → (4) plist →
(5) SHA-512 역추적 순으로 시도합니다. 5번은 비용이 크므로 **앞 단계가 모두 실패했을 때만** 실행되고,
성공하면 캐시에 저장되어 다음 기동은 즉시 끝납니다. plist에 직접 `userId`가 없는 환경에서는 5번이
실제 경로가 됩니다(수십 초 소요).

아카이브는 **SQLCipher로 전체 암호화**됩니다. 키는 최초 실행 시 생성되어 macOS 로그인 키체인에
저장되고, 그때 화면에 한 번 표시됩니다.

```bash
sesametok-backend archive-key --force   # 64자 hex 키를 출력
```

출력된 키를 **비밀번호 관리자 등에 별도로 보관하세요.** 키를 잃으면 아카이브를 복호화할 수 없고,
카카오톡 원본 DB에서 다시 적재해야 합니다. 헤드리스/테스트 환경에서는 `SESAMETOK_ARCHIVE_KEY`로
같은 키를 주입할 수 있습니다.

### 권한 (가장 흔한 함정)

권한은 "책임 프로세스"에 귀속되고, **실행 방법과 실행 파일 경로에 따라 대상이 달라집니다.**

| 실행 방법 | 권한을 부여할 대상 |
|---|---|
| 터미널에서 `foreground` | `sesametok-backend status`가 알려주는 경로(보통 터미널 또는 venv python이 resolve된 실제 바이너리) |
| launchd | `ProgramArguments`의 절대 경로 python |

- `./.venv/bin/python`은 pyenv 바이너리의 심볼릭 링크일 수 있습니다. 이때 macOS는 **resolve된 실제
  바이너리 경로**로 권한을 기록하므로, Python 버전을 올리면 권한이 무효가 됩니다. `status`가
  `launched as`와 `grant target`을 함께 보여줍니다.
- **전체 디스크 접근**: 카카오톡 DB 읽기에 필요. `sesametok-backend permissions`
- **손쉬운 사용**: 발신에 필요. `sesametok-backend permissions --accessibility`

---

## 로컬 API

- 바인딩: `127.0.0.1:8756`(기본) + Unix 도메인 소켓 `<data_dir>/backend.sock`(0600)
- 인증: `Authorization: Bearer <API_TOKEN>` — `/health`만 토큰 없이 허용
- 시간: 전부 RFC3339 UTC. 표시 타임존 변환은 인터페이스 책임
- 오류: `{"error": {"code", "message", "retryable"}}`

| 메서드/경로 | 역할 |
|---|---|
| `GET /health` | 생존/준비 상태(토큰 불필요) |
| `GET /v1/status` | 진단(권한·폴링·아카이브·webhook) |
| `GET /v1/rooms` | 방 목록 |
| `GET /v1/rooms/{chat_id}` | 방 단건 |
| `GET /v1/rooms/{chat_id}/messages` | 대화 조회 `?since=&until=&limit=&before=` |
| `GET /v1/messages/{message_id}` | 메시지 단건 |
| `GET /v1/events` | SSE. `Last-Event-ID`/`?since=`로 재개 |
| `POST /v1/messages` | 발신/초안/dry-run |
| `POST /v1/sync` | 즉시 1회 동기화 |
| `GET/POST /v1/webhooks`, `PATCH/DELETE /v1/webhooks/{id}` | webhook 구독 관리 |

```bash
TOKEN=$(sesametok-backend token)
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8756/v1/rooms
curl -s -H "Authorization: Bearer $TOKEN" 'http://127.0.0.1:8756/v1/rooms/100/messages?limit=20'
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"chat_id":"100","text":"안녕하세요","mode":"dry_run"}' \
  http://127.0.0.1:8756/v1/messages
```

### 발신 계약

- `mode`: `dry_run`(해석만) · `draft`(입력창에 초안만) · `send`(실제 전송)
- `send`/`draft`는 `accept_use_policy: true`가 없으면 거부됩니다. `dry_run`은 예외입니다.
- `client_request_id`로 멱등성이 보장됩니다. 같은 키 재요청은 이전 결과를 반환하고 다시 보내지 않습니다.
- 동시에 2건이 나가지 않도록 발신 워커가 직렬화하고, 분당 상한이 적용됩니다.
- 이미지는 `<data_dir>/outbox` 안의 파일만 보낼 수 있습니다(경로 탈출 차단).
- 결과가 `unconfirmed`이면 **재시도하지 마세요.** 중복 전송이 될 수 있습니다.

### Webhook push (제한사항)

- **대상은 loopback(`127.0.0.1`/`::1`/`localhost`)의 `http` URL만** 허용합니다. 포트를 명시해야 합니다.
- 전달은 at-least-once, 지수 백오프 재시도(기본 5회), 본문에 대한
  `X-Sesametok-Signature: sha256=<HMAC-SHA256(secret, body)>` 서명을 포함합니다.
- 구독별 `cursor_seq`로 재개하며, 실패 시 `last_error`에 기록됩니다.
- **외부 메신저로의 전달은 인터페이스 프로젝트의 책임입니다.** 이 백엔드는 외부 네트워크로
  나가지 않습니다. Slack/Telegram 어댑터가 로컬에서 webhook을 받아 각 플랫폼으로 보내세요.
- secret은 생성 응답에서 **한 번만** 반환됩니다.

---

## 보안

- 아카이브(`<data_dir>/archive.sqlite3`)는 SQLCipher 전체 파일 암호화. 데이터 디렉터리 0700, 파일 0600.
- 로그에는 개수·id·상태·문자 수만 남깁니다. 메시지 본문·방 이름·닉네임·키는 남기지 않습니다.
  긴 hex/base64 형태는 로그 필터가 `[redacted]`로 치환합니다.
- 카카오톡 원본 DB는 **읽기 전용**으로만 열고 수정하지 않습니다(checkpoint/vacuum/lock 조작 금지).
- API는 loopback 전용입니다. 인터넷에 노출하지 마세요(토큰이 있어도).
- 실제 대화에서 유래한 값을 코드·테스트·문서·커밋에 남기지 않습니다. 테스트는 합성 데이터만 사용합니다.

---

## 설정(환경변수)

| 변수 | 의미 | 기본 |
|---|---|---|
| `SESAMETOK_API_TOKEN` | 인터페이스 토큰(미설정 시 파일에서 생성) | 자동 생성 |
| `SESAMETOK_HOST` / `SESAMETOK_PORT` | API 바인딩 | `127.0.0.1` / `8756` |
| `SESAMETOK_DATA_DIR` / `SESAMETOK_LOG_DIR` | 데이터·로그 경로 | `~/Library/Application Support/sesametok-backend` · `~/Library/Logs/sesametok-backend` |
| `SESAMETOK_ARCHIVE_KEY` | 아카이브 키(64 hex) override | 키체인 |
| `SESAMETOK_USER_ID` | 계정 user_id override | 자동 탐지 |
| `SESAMETOK_POLL_SECONDS` / `SESAMETOK_LOOKBACK_SECONDS` | 폴링 주기 / 수정 감지 창 | `3` / `900` |
| `SESAMETOK_BACKFILL_DAYS` / `SESAMETOK_BACKFILL_MAX` | 최초 백필 상한 | `30` / `100000` |
| `SESAMETOK_EVENT_RETENTION_DAYS` / `..._MAX` | 이벤트 보존 | `30` / `100000` |
| `SESAMETOK_MAX_SENDS_PER_MINUTE` / `..._PER_ROOM_PER_MINUTE` | 전송 레이트 리밋 | `6` / `3` |
| `SESAMETOK_WEBHOOK_TIMEOUT_SECONDS` / `..._MAX_ATTEMPTS` | webhook 전달 | `5` / `5` |
| `SESAMETOK_UDS` / `SESAMETOK_UDS_PATH` | UDS 사용 / 경로 | `1` / `<data_dir>/backend.sock` |
| `SESAMETOK_LAUNCHD_PLIST` | plist 경로 override | `~/Library/LaunchAgents/com.sesametok.sesametok-backend.plist` |
| `SESAMETOK_API` | API·webhook·발신 워커 기동 | `1` |
| `SESAMETOK_NO_MENUBAR` | 메뉴바 없이 서버만 실행 | `0` |
| `SESAMETOK_LOG_LEVEL` | 로그 레벨 | `info` |

---

## 개발

```bash
./.venv/bin/python -m pytest          # 전체 테스트(합성 데이터만)
./.venv/bin/python -m pytest -q tests/test_reader_synthetic_db.py
```

- 테스트는 실제 카카오톡 DB·키체인·launchd에 의존하지 않습니다.
- 합성 SQLCipher DB 픽스처: `tests/support.py`
- 개발 실행 예: `SESAMETOK_DATA_DIR=$PWD/.devdata SESAMETOK_ARCHIVE_KEY=<64hex> \
  SESAMETOK_BACKFILL_DAYS=7 ./bin/sesametok-backend foreground`
- 구조: `src/sesametok_backend/{api,app,core,kakao,send,security}`

## 문제 해결

| 증상 | 확인 |
|---|---|
| 카카오톡 DB를 못 찾음 | `sesametok-backend status`의 계정/경로, `SESAMETOK_USER_ID` 설정 |
| `file is not a database` | 키 유도 입력(user_id/uuid) 불일치. `SESAMETOK_USER_ID` 확인 |
| 권한을 줬는데도 안 됨 | 실행 방법/경로가 바뀌었는지 확인 후 재부여 |
| 발신이 권한 오류 | `sesametok-backend permissions --accessibility` |
| 아카이브가 안 열림 | `SESAMETOK_ARCHIVE_KEY`가 맞는지, 키체인 접근 가능 여부 확인 |
