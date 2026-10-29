# 클라이언트 프로젝트 통합 가이드 (sesametok-backend)

이 문서는 **이 백엔드를 추상화(=인터페이스) 레이어로 사용할 클라이언트 프로젝트**를 시작하는
사람과 코딩 에이전트를 위한 것입니다. 무엇이 이미 구현되어 있고, 어떤 계약을 지켜야 하며,
무엇을 이 프로젝트에 맡기면 안 되는지를 **구현 수준에서** 설명합니다.

- 제품 개요·설치: `README.md`
- 확정된 결정과 근거: `DECISIONS.md` (ADR-002 ~ ADR-021, **ADR-001은 삭제됨**)
- 이 저장소에서 작업하는 규칙: `AGENTS.md`
- 코드: `src/sesametok_backend/`
- ⚠️ **`HANDOFF_MESSAGING.md`는 역사 문서입니다.** 여기에는 지금은 제거된 outbox·이미지 발신
  설계가 남아 있으니 **따라 하지 마세요**(14절 참고).

> **한 줄 요약**: macOS에 로그인된 카카오톡 데스크톱 앱에서 **읽기**는 로컬 SQLCipher DB를
> 읽어 암호화 아카이브로 재구성하고 loopback API/SSE/webhook으로 노출하며, **쓰기(발신)** 는
> 이미 열려 있는 채팅 창에 대해 macOS Accessibility로 **텍스트만** 전송합니다.

---

## 1. 이 프로젝트가 하는 것 / 하지 않는 것

| 구분 | 내용 |
|---|---|
| **한다** | 카카오톡 DB 읽기(읽기 전용), 30일/10만건 백필, 폴링으로 신규·수정 감지, 암호화 아카이브, 방·메시지 조회 API, SSE 이벤트 스트림, loopback webhook push, **텍스트 발신**(dry-run/초안/실제), 메뉴바 상태 아이콘 |
| **하지 않는다** | 채팅 UI, 메신저 어댑터(Slack/Telegram 등), 외부 네트워크 통신, 이미지·파일·이모티콘 **발신**, 미디어 **수신/저장**, 카카오톡 창 자동 열기, 카카오톡 DB 수정, LOCO(카카오톡 자체 프로토콜) |

**경계선**: 이 백엔드는 "로컬 카카오톡의 상태를 안정적인 계약으로 노출하는 서버"입니다.
Slack/Telegram/TUI 같은 인터페이스는 **클라이언트 프로젝트**가 만들며, 그쪽이 이 API를 소비하고
외부 플랫폼으로 팬아웃합니다.

---

## 2. 전체 구조

```
┌──────────────────────────────┐
│ KakaoTalk.app (로그인된 상태) │
│  ├─ NTChatRoom / NTMessage…  │  ← 읽기 전용(SQLCipher, mode=ro)
│  └─ 열려 있는 채팅 창(UI)     │  ← AX로 텍스트 입력 + 전송
└───────────┬──────────────────┘
            │ (읽기)                          (쓰기)
   kakao/derive.py  키 유도                     send/accessibility.py
   kakao/auth.py    계정 식별                    send/targets.py
   kakao/cipher.py  복호화 연결                  core/send_queue.py
   kakao/reader.py  정규화
   kakao/source.py  스냅샷
            │
     core/poller.py  (주기 폴링, lookback 900초)
            │  fingerprint diff
     core/archive.py (자체 SQLCipher 아카이브: rooms/messages/events/webhooks/send_requests)
            │
   ┌────────┴─────────┐
   │ api/server.py    │  HTTP 127.0.0.1:8756 + UDS <data_dir>/backend.sock
   │  ├─ router       │
   │  ├─ sse          │
   │  └─ webhooks     │  loopback http로 push (HMAC 서명)
   └──────────────────┘
            ↕
     **클라이언트 프로젝트** (Slack/Telegram/TUI…)
```

### 모듈 책임

| 경로 | 책임 | 클라이언트가 알아야 할 것 |
|---|---|---|
| `config.py` | 환경변수 → 모든 경로/설정 | 경로·주기·레이트 리밋의 단일 출처 |
| `kakao/` | 저장소 탐색, 키 유도, 복호화, 계정 식별, 정규화 | **읽기 전용.** 여기서 DB를 쓰지 않는다 |
| `core/poller.py` | 주기적 스냅샷 + diff → 이벤트 | 폴링 주기는 서버가 관리(메뉴바에서 변경 가능) |
| `core/archive.py` | 자체 암호화 아카이브, 페이지네이션, 이벤트, webhook, 발신 요청 기록 | API가 노출하는 모든 조회의 원천 |
| `core/send_queue.py` | **단일 워커** 발신 큐, 멱등성, 레이트 리밋 | 동시에 1건만 나간다. `unconfirmed`는 재시도 금지 |
| `send/` | AX 드라이버(창 매칭·입력·전송), 대상 해석 | **이미 열려 있는 창에만** 전송 |
| `api/` | HTTP/UDS, 인증, SSE, webhook, 오류 계약 | 계약의 유일한 입구 |
| `app/` | 메뉴바(PyObjC) — 상태·일시정지·주기·종료 | API가 아니라 사람이 쓰는 표면 |
| `launcher.py` | ProcessLock(단일 인스턴스), LaunchAgent | `start/stop/restart`의 실제 동작 |
| `security/` | API 토큰 파일, 아카이브 키(Keychain) | **절대 출력/로그 금지** |
| `diagnostics.py` | 권한·실행 파일 진단 | `status`의 근거, 권한 판정 방식 |

### 실행 모델 (중요)

- 운영은 **launchd LaunchAgent**가 소유합니다: `~/Library/LaunchAgents/com.sesametok.sesametok-backend.plist`.
- plist의 `ProgramArguments`는 **`/Applications/SesametokBackend.app/Contents/MacOS/SesametokBackend --managed`**
  (앱이 없으면 인터프리터 직접 실행으로 폴백).
- 앱은 **얇은 스텁**입니다. 파이썬을 번들하지 않고 `Contents/Resources/target`의 인터프리터를
  **자식으로 spawn**하고 부모로 남습니다. 그래야 TCC 권한이 앱에만 귀속됩니다(ADR-015).
- 앱을 **더블클릭하면 `sesametok-backend start`와 같습니다**.
- 단일 인스턴스는 `<data_dir>/backend.lock`의 `flock`으로 보장합니다.

---

## 3. 데이터 모델

### Room

| 필드 | 타입 | 설명 |
|---|---|---|
| `chat_id` | string | 카카오톡 내부 채팅 id. **API의 기본 키** |
| `chat_name` | string | 표시 이름(아래 규칙) |
| `chat_type` | `"direct"` \| `"group"` | type 0/2, `directChatMemberUserId`, 활성 멤버 2명 중 하나로 판정 |
| `last_message_at` | RFC3339 UTC \| null | 아카이브 기준 마지막 메시지 시각 |
| `message_count` | int | 아카이브에 보관된 메시지 수 |

**`chat_name` 생성 규칙**(`kakao/reader.py`):

1. `NTChatRoom.chatName`이 있으면 그것.
2. 없으면 `NTChatMeta` 제목.
3. 오픈링크 이름.
4. 1:1이면 상대 닉네임(`friendNickName → displayName → nickName`).
5. 그룹이면 구성원 닉네임을 `, `로 이어붙이되 **최대 8명**(`MAX_GROUP_NAME_MEMBERS`).
6. 해석 못 한 멤버는 `user-<id>`로 표시.
7. 아무것도 없으면 `chat-<chatId>`.

> **함정 1**: `chat_name`은 카카오톡 UI 제목과 **다를 수 있습니다**(특히 그룹·자기 채팅).
> **이름을 식별자로 쓰지 말고 `chat_id`를 쓰세요.**
> **함정 2**: **오픈채팅 방은 아카이브에서 제외**됩니다(`NTChatRoom.linkId` 기준).
> **함정 3**: `/v1/rooms`는 `chat_id` 오름차순이며 **페이지네이션이 없습니다**(방 수만큼 한 번에).

### Message

| 필드 | 타입 | 설명 |
|---|---|---|
| `message_id` | string | 메시지 id(문자열, `<chat_id>-<log_id>` 형태) |
| `chat_id` / `chat_name` / `chat_type` | string | 소속 방 |
| `sender_id` / `sender_nickname` | string | 발신자. 닉네임 미해석 시 `user-<id>` |
| `timestamp` | RFC3339 UTC | 카카오톡이 기록한 시각 |
| `text` | string | 본문(대체 문구일 수 있음) |
| `message_type` | string | **`"text"` 또는 `"type_<N>"`** |
| `reply_to_message_id` | string \| null | 답글 대상 |
| `is_feed` | bool | 시스템/피드성 메시지 |
| `original_message_type` | string \| null | 대체된 경우 원래 타입 |
| `is_placeholder` | bool | 본문이 대체 문구인지 |

**타입 판정**(`kakao/reader.py`):

- `message_type == "text"` → 사람이 쓴 텍스트.
- `message_type == "type_0"` + `is_feed=True` → 피드/시스템 메시지.
  ⚠️ **feed의 `text`는 사람이 읽는 문장이 아니라 원본 JSON 문자열**일 수 있습니다.
  렌더링할 때 `is_feed`를 확인하세요.
- 지원하지 않는 타입 → `message_type="text"`, `is_placeholder=True`,
  `original_message_type="type_<raw>"`, 본문은 아래 고정 문구.

미지원 타입 대체 문구(`models.PLACEHOLDER_TEXT`):

```
[지원하지 않는 메시지 타입입니다. 원본 메시지를 확인해주세요.]
```

> 본문을 그대로 사용자에게 보여줄지 판단할 때는 `is_placeholder`/`is_feed`를 쓰세요.
> **`message_type`에 `"feed"`/`"placeholder"` 같은 값은 존재하지 않습니다.**

### Event

| 필드 | 타입 | 설명 |
|---|---|---|
| `seq` | int | **단조 증가**, 최상위 필드. 재개의 기준 |
| `kind` | string | `message.created` / `message.updated` / `room.updated` / `send.result` |
| `payload` | object | 아래 표 |
| `created_at` | RFC3339 UTC | 아카이브 기록 시각 |

| kind | payload |
|---|---|
| `message.created`, `message.updated` | `Message` 객체 전체 |
| `room.updated` | `Room` 객체 전체 |
| `send.result` | `client_request_id`, `chat_id`, `mode`, `result`, `chars`, `code` |

> `payload` 안에는 `seq`가 **없습니다**. 멱등 키로 쓰려면 **본문 최상위 `seq`** 또는
> webhook 헤더 `X-Sesametok-Event-Seq`를 쓰세요.
> `send.result`의 payload에는 **본문이 없습니다**(로그·이벤트에 본문을 남기지 않는 원칙).

### 시간 규칙 (매우 중요)

- 모든 시각은 **RFC3339 UTC, `+00:00` 표기, 초 단위**(예: `2026-10-19T08:06:39+00:00`).
- `since`/`until`은 DB에서 **문자열 비교**입니다(`timestamp >= ?`, `timestamp < ?`).
- 따라서 **반드시 `...+00:00` 형식을 그대로 쓰세요.**
  `Z` 접미사나 `+09:00`을 보내면 문자열 비교가 깨져 **조용히 빈 결과**가 나옵니다.
- **`since`는 포함(inclusive), `until`은 배타(exclusive)** 입니다.
- 표시 타임존 변환은 **클라이언트 책임**입니다.

### 보존 정책

- **이벤트만** 30일/100,000건으로 prune 됩니다(`event_retention_*`).
- **방·메시지는 자동 삭제되지 않습니다.** 아카이브가 커지면 디스크를 계속 사용합니다.
- `send_requests`, `webhooks`도 prune 되지 않습니다.

---

## 4. API 스펙

### 4.1 전송·인증·공통 규약

- 베이스 URL: `http://127.0.0.1:8756` (기본). `SESAMETOK_HOST/PORT`로 변경 가능.
- **Unix 도메인 소켓**: `<data_dir>/backend.sock` (0600). HTTP를 그대로 UDS로 서비스합니다.

  ```bash
  curl --unix-socket ~/Library/Application\ Support/sesametok-backend/backend.sock http://localhost/health
  ```

  - 경로가 **100바이트를 넘으면 UDS를 조용히 비활성화**하고 경고 로그만 남깁니다(HTTP는 정상).
    `SESAMETOK_UDS_PATH`로 줄이거나 `SESAMETOK_UDS=0`으로 끌 수 있습니다.
- 인증: `Authorization: Bearer <API_TOKEN>`. **`GET /health`만 토큰이 필요 없습니다.**
- 토큰 얻기: `sesametok-backend token` (파일 `<data_dir>/api_token`, 0600).
- **본문 상한 2 MiB**(`MAX_BODY_BYTES`). 판정은 **`Content-Length` 헤더만** 보며,
  초과하면 **본문을 읽지 않고** `413 payload_too_large`를 반환합니다(연결이 닫힐 수 있으니
  큰 본문을 계속 보내지 마세요).
  `Content-Length`가 없는(chunked) 요청은 본문이 빈 것으로 처리됩니다.
- **`Content-Type`은 검증하지 않습니다**(415 응답은 존재하지 않음). `application/json`을 쓰세요.
- 허용되지 않은 메서드: `405` + `{"error":{"code":"method_not_allowed",…}}`(JSON).
- **loopback 전용**입니다. 인터넷에 노출하지 마세요.

오류 응답 형식(모든 실패):

```json
{"error": {"code": "window_not_open", "message": "사람이 읽을 설명", "retryable": false}}
```

공통 상태 코드:

| HTTP | code | 의미 |
|---|---|---|
| 400 | `bad_request` | 파라미터/본문 오류 |
| 401 | `unauthorized` | 토큰 없음/불일치 |
| 404 | `route_not_found` | 없는 경로 |
| 404 | `room_not_found` / `not_found` | 없는 리소스 |
| 405 | `method_not_allowed` | 메서드 불일치 |
| 409 | `event_replay_unavailable` / `unconfirmed` 등 | 상태 충돌 |
| 413 | `payload_too_large` | 본문 2 MiB 초과 |
| 500 | `internal_error` | 미처리 예외 |
| 501 | `not_implemented` | 미구현 |
| 502 | (발신 계열, 4.6) | UI/외부 실패 |
| 503 | `send_unavailable` / `poller_unavailable` / `not_running` / `accessibility_denied` | 일시적 불가 |

> ⚠️ **`retryable` 필드를 재시도 판단의 근거로 쓰지 마세요.** 실제로는 `internal_error`와
> `poller_unavailable` 정도만 `true`이고, **429 `rate_limited`, 409 `unconfirmed`도 `false`**입니다.
> 재시도 여부는 아래 "실패 시 행동" 표(6.4절)를 따르세요.

### 4.2 엔드포인트

#### `GET /health` (토큰 불필요)

```json
{"ok": true, "kakaotalk_running": true, "permissions_ok": true,
 "polling": true, "last_sync_at": null, "consecutive_failures": 0}
```

- `ok`는 `running && consecutive_failures == 0`입니다. **폴링 연속 실패면 `false`**입니다.
- `permissions_ok`는 **읽기 경로가 동작하는지**입니다. 발신 가능 여부와 다릅니다.
- ⚠️ `kakaotalk_running`/`permissions_ok`는 **기동 시점에 잰 스냅샷**입니다(라이브 재측정 아님).
- **발신 준비 판정에 쓰지 마세요.**

#### `GET /v1/status`

실제 응답 키: `uptime_seconds`, `archive`, `polling`, `webhooks`, `version`, `account`, `permissions`.

```json
{
  "uptime_seconds": 1234,
  "archive": {"rooms": 411, "messages": 11455, "event_seq": 1926,
              "oldest_event_seq": 0, "path": "…/archive.sqlite3"},
  "polling": {"polls": 64, "created": 0, "updated": 0, "rooms_changed": 0,
              "skipped_rows": 0, "last_poll_at": "…", "last_success_at": "…",
              "last_duration_ms": 120, "consecutive_failures": 0,
              "last_error": null, "backfilled": true},
  "webhooks": {"total": 1, "active": 1},
  "version": "0.1.0",
  "account": {"source": "cache", "user_id_digits": 9, "uuid_shape": "36-chars"},
  "permissions": {
    "accessibility_trusted": true, "store_readable": true, "full_disk_access": false,
    "grant_target": "/Applications/SesametokBackend.app", "grant_target_is_app": true,
    "python_executable": "…", "invoked_executable": "…", "python_is_symlinked": true,
    "kakaotalk_installed": true, "kakaotalk_running": true, "menubar": true,
    "store_roots": [ … ]
  }
}
```

- ⚠️ **`polling.poll_seconds`는 없습니다.** 실제 주기는 메뉴바(사용자) 또는
  `SESAMETOK_POLL_SECONDS`로만 바뀝니다.
- ⚠️ **발신 통계 필드(`send`)는 API에 노출되지 않습니다.** 발신 상태는 **실제 발신 응답**과
  `send.result` 이벤트로만 알 수 있습니다.
- `permissions`도 **기동 시점 스냅샷**입니다. 최신 판정이 필요하면 백엔드를 재시작하거나
  CLI `sesametok-backend status`를 쓰세요(실행 중이면 백엔드 값을, 아니면 터미널 값을 표시).
- `version`으로 클라이언트의 최소 지원 버전을 확인하세요. **경로는 `/v1`로 고정**입니다.

#### `GET /v1/rooms`

```json
{"rooms": [{"chat_id": "…", "chat_name": "…", "chat_type": "direct",
            "last_message_at": "2026-10-19T06:46:02+00:00", "message_count": 3}]}
```

#### `GET /v1/rooms/{chat_id}`

`Room` 객체 하나. 없으면 `404 room_not_found`.

#### `GET /v1/rooms/{chat_id}/messages`

쿼리: `since`(포함), `until`(배타), `limit`, `before`.

```json
{"chat_id": "…", "messages": [ … ], "next_before": "…", "has_more": true}
```

- **가장 최신 페이지부터** 주고, **페이지 안에서는 오래된 → 최신** 순서입니다.
- `limit`: 기본 **100**, `> 1000`은 **오류가 아니라 1000으로 절삭(clamp)**.
  `0` 이하/비정수는 `400 bad_request`.
- `before`는 **같은 방의 `message_id` 앵커**이며 배타(`(timestamp, message_id) <`)입니다.
  앵커가 그 방에 없으면 **빈 페이지 + `has_more:false`**를 **200**으로 반환합니다(오류 아님).
- `next_before`는 그 페이지에서 **가장 오래된** 메시지 id입니다. 더 과거로 갈 때 넘기세요.
- `since`/`until` 형식은 3절 "시간 규칙"을 반드시 지키세요.

#### `GET /v1/messages/{message_id}`

`Message` 객체 하나. 없으면 `404`.

#### `GET /v1/events` (SSE)

- `Content-Type: text/event-stream`.
- `Last-Event-ID` 헤더 또는 `?since=<seq>`로 재개합니다.
- **아무것도 주지 않으면 현재 헤드에서 시작**합니다(백로그 재생 강제 없음).
- 프레임:

```
id: 1927
event: message.created
data: {"seq":1927,"kind":"message.created","payload":{…},"created_at":"…"}

```

- keep-alive 주석은 **`": ping"`** 입니다(15초 간격). 문자열에 의존하지 말고 무시하세요.
- **`409 event_replay_unavailable`은 이벤트가 prune 된 뒤에만** 발생합니다
  (`oldest_event_seq > 0` 이고 `since + 1 < oldest`). prune 전에는 `?since=0`이 **전체 재생**입니다.
- 409를 받으면: REST로 히스토리를 채우고 **헤드에서 다시 구독**하세요.
- ⚠️ **이벤트 버스는 구독자당 큐가 2000개**이고, 넘치면 **가장 오래된 이벤트를 버립니다**
  (`core/events.py`). 느린 소비자·긴 재생 중에는 **seq 갭이 생길 수 있습니다.** 5.2절의
  갭 감지 절차를 구현하세요.

#### `POST /v1/messages` (발신)

요청:

```json
{"chat_id": "…", "text": ".......", "mode": "dry_run",
 "accept_use_policy": false, "client_request_id": "uuid-…", "background_only": false}
```

| 필드 | 필수 | 설명 |
|---|---|---|
| `chat_id` | ✅ | 대상 방. 아카이브에 있어야 함 |
| `text` | **`send`에서만 필수** | 최대 20,000자. **이미지 필드는 제거됨** |
| `mode` | | `dry_run` \| `draft` \| `send` (기본 `send`) |
| `accept_use_policy` | `send`/`draft` | `true` 없으면 `400 no_consent` |
| `client_request_id` | 권장 | 멱등성 키 |
| `background_only` | | `true`면 백그라운드 전용 경로(`draft`는 거부) |

> `draft`에 `text`를 생략하면 **빈 초안**이 붙여넣어지고 `drafted`가 반환될 수 있습니다.
> 클라이언트가 빈 텍스트를 먼저 검증하세요.

**성공 응답(200)**:

```json
{"result": "sent", "room": "…", "chars": 7, "client_request_id": "…"}
```

`result`: `sent` | `drafted` | `unconfirmed` | `dry_run`. `chars`는 `len(text)`입니다.

**⚠️ `unconfirmed`는 두 가지 형태로 옵니다 — HTTP 상태만 보고 판정하면 안 됩니다.**

| 상황 | HTTP | 본문 |
|---|---|---|
| 워커가 UI 실패를 보고 | **200** | `{"result":"unconfirmed","error":{"code":"not_sent"\|"background_send_not_accepted",…}}` |
| API가 90초 안에 결과를 못 받음 | **409** | `{"error":{"code":"unconfirmed","retryable":false,…}}` |
| 같은 `client_request_id`가 아직 pending | **409** | `{"error":{"code":"unconfirmed",…}}` |

→ **판정은 `result`와 `error.code`로 하세요.** 어느 경우든 **재시도 금지**입니다.

**`client_request_id` 재사용 시 응답(200)**:

```json
{"result": "sent", "room": null, "chars": 0, "client_request_id": "…",
 "error": {"code": "duplicate_request", "message": "이미 처리되었으므로 저장된 결과를 반환합니다.", "retryable": false}}
```

→ 성공이지만 `room`/`chars`는 **신뢰하지 마세요**(저장된 결과만 반환).

발신 규칙(반드시 숙지):

1. **대상 방 창은 사용자가 열어 두어야 합니다.** 백엔드는 창을 열지 않고, 채팅 목록·검색 상자도
   건드리지 않습니다(ADR-021). 창이 없으면 **`502 window_not_open`** 이고 아무것도 전송되지 않습니다.
2. 창이 열려 있으면 **포커스를 뺏지 않고** 전송합니다(다른 앱 뒤에 가려져 있어도 그대로).
3. **`dry_run`은 아무것도 하지 않습니다.** 대상 해석만 하고, 레이트 리밋을 소비하지 않으며
   `send.result` 이벤트도 만들지 않습니다.
4. **`draft`는 입력창에 붙여넣습니다.** 카카오톡이 앞으로 나오고 포커스가 입력창으로 갑니다.
   구버전 빌드에서는 `AXValue` 쓰기 자체가 전송일 수 있으므로 **`draft`를 "전송 안 됨"으로
   간주하면 안 됩니다.**
5. **`unconfirmed`는 절대 자동 재시도하지 마세요.** 중복 전송이 됩니다.
6. `client_request_id`를 재사용하면 이전 결과가 반환되고 **다시 보내지 않습니다.**
   같은 키로 다른 본문을 보내는 것은 계약 위반입니다.
7. 동시에 **1건만** 나갑니다(단일 워커).
   레이트 리밋 기본값: **전체 60건/분, 방별 50건/분**(`SESAMETOK_MAX_SENDS_PER_MINUTE`,
   `..._PER_ROOM_PER_MINUTE`로 변경).
8. API는 결과를 최대 **90초** 기다립니다(`DEFAULT_WAIT_SECONDS`). 그 안에 끝나지 않으면 409
   `unconfirmed`입니다.

발신 오류 코드(전체):

| HTTP | code | 의미 |
|---|---|---|
| 400 | `bad_request` | `chat_id` 없음, `send`에 `text` 없음, 20,000자 초과 |
| 400 | `bad_mode` | `mode`가 3종이 아님 |
| 400 | `no_consent` | `accept_use_policy` 누락 |
| 400 | `room_name_not_typable` | 방 이름에 제어/보이지 않는 문자 포함 |
| 404 | `room_not_found` | 아카이브에 그 `chat_id`가 없음 |
| 409 | `unconfirmed` | 결과 미확인(타임아웃/pending). **재시도 금지** |
| 409 | `target_ambiguous` | 같은 이름 + 같은 최신 시각의 방이 여럿 |
| 409 | `ambiguous_room` | 같은 이름의 창이 여럿 열려 있음 |
| 429 | `rate_limited` | 분당 상한 초과 |
| 502 | `window_not_open` | 대상 창이 열려 있지 않음 |
| 502 | `compose_box_not_found` / `set_value_failed` / `draft_not_staged` / `value_mismatch` | UI 조작 실패 |
| 502 | `background_only_unsupported` | `draft` + `background_only` 조합 |
| 502 | `user_busy` / `focus_lost` | **`draft` 경로에서만** 발생(붙여넣기 전 최전면/입력 대기) |
| 503 | `send_unavailable` | 발신 워커 미구성. **`dry_run`도 이 코드로 거부됩니다** |
| 503 | `not_running` | 카카오톡이 실행 중이 아님 |
| 503 | `accessibility_denied` | 손쉬운 사용 권한 없음 |

> **표에 없는 발신 코드는 전부 502로 나갑니다**(예: `kakaotalk_not_running`).
> **`room_open_failed`·`search_unavailable`·`chat_list_*`·`room_not_in_chat_list`는 자동 열기
> 제거로 도달 불가능**합니다(템플릿만 남아 있음).
> **`cancelled`·`cancelled_after_paste`도 현재 발생하지 않습니다**(커튼 코드는 잔존하지만 미사용).

#### `POST /v1/sync`

즉시 1회 폴링을 요청합니다. 응답은 **`202` + `{"result": "scheduled"}`** 이고, 실제 결과는
SSE/webhook으로 관찰합니다. 폴러가 없으면 `503 poller_unavailable`(retryable=true)입니다.

#### Webhook CRUD

| 메서드 | 경로 | 설명 |
|---|---|---|
| `GET` | `/v1/webhooks` | 목록(`secret` 제외) |
| `POST` | `/v1/webhooks` | 생성. **`secret`은 이 응답에서 한 번만** 반환 |
| `PATCH` | `/v1/webhooks/{id}` | `{"active": true|false}` |
| `DELETE` | `/v1/webhooks/{id}` | 삭제 |

```json
{"url": "http://127.0.0.1:9000/kakao", "kinds": ["message.created"], "secret": "16자 이상(선택)"}
```

```json
{"id": "uuid", "url": "…", "kinds": [ … ], "active": true, "cursor_seq": 1926,
 "secret": "…", "signature_header": "X-Sesametok-Signature: sha256=<hmac-sha256(secret, body)>"}
```

- URL은 **loopback(`127.0.0.1`/`::1`/`localhost`)의 `http`**, 포트 명시 필수.
- `kinds` 생략 시 **4종 전체**가 기본값입니다. 값은 위 4종의 부분집합.
- `cursor_seq`는 **생성 시점의 현재 헤드**입니다. **과거 이벤트는 재생하지 않습니다.**
  히스토리는 REST로 채우세요.
- 전달 요청 헤더:

  | 헤더 | 값 |
  |---|---|
  | `X-Sesametok-Signature` | `sha256=<HMAC-SHA256(secret, body_bytes)>` |
  | `X-Sesametok-Event-Seq` | 이벤트 `seq` |
  | `X-Sesametok-Event-Kind` | 이벤트 `kind` |

- 본문은 **`Event.to_dict()` 전체**이며 `seq`·`kind`가 **최상위**에 있습니다.
- 전달은 **시도 기준 at-least-once**입니다. 지수 백오프로 **기본 5회** 시도하고,
  그래도 실패하면 **그 이벤트를 버리고 `cursor_seq`를 전진**시킵니다(60초 뒤 재개).
  → **이벤트가 유실될 수 있으므로** `cursor_seq`/`last_error`를 주기적으로 확인하고
  REST로 갭을 보정하세요.
- 시도 횟수/백오프 상태는 **메모리**에 있어 백엔드 재시작 시 초기화됩니다.

---

## 5. 클라이언트 통합 패턴 (권장)

### 5.1 초기 동기화 — **백필은 이벤트를 만들지 않습니다**

```
1) GET /health                 # 생존 확인(토큰 불필요)
2) GET /v1/status              # version, account, archive.event_seq(헤드), oldest_event_seq
3) GET /v1/rooms               # 방 목록
4) 방별 히스토리: GET /v1/rooms/{chat_id}/messages?limit=…&before=…  (필요한 만큼)
5) GET /v1/events?since=<2에서 읽은 event_seq>   # 그 이후분만 구독
```

**⚠️ 가장 중요한 사실**: 최초 기동/백필 구간에는 **`message.created` 이벤트가 생성되지 않습니다**
(`poll_once()`가 백필 중에는 발행하지 않음). 따라서

- 초기 히스토리는 **반드시 REST로** 채워야 합니다.
- `event_seq == 0`은 "이벤트 없음"이 아니라 **"백필만 됨"** 일 수 있습니다.
- 백필 이후의 신규/수정만 SSE·webhook으로 들어옵니다.

**순서**: SSE를 먼저 열고 히스토리를 채우면 그 사이 구간을 놓칠 수 있습니다.
`status.event_seq`를 읽고 → REST로 히스토리 → `since=event_seq`로 구독하세요(그 사이 이벤트는
재생으로 따라잡습니다).

### 5.2 중복 제거와 **갭 감지**

- 메시지 식별자는 `message_id`. `message.updated`는 **같은 id를 갱신**하므로 upsert로 처리하세요.
- webhook은 at-least-once(시도 기준)이므로 **본문 최상위 `seq`**(또는
  `X-Sesametok-Event-Seq` 헤더)로 멱등 처리합니다. `payload.seq`는 없습니다.
- **갭 감지(필수)**: 받은 `seq`가 `이전 seq + 1`인지 확인하세요. 갭이 보이면
  ① `Last-Event-ID`로 재접속하고 ② REST로 해당 구간을 보정합니다.
  (이벤트 버스 큐가 2000개를 넘으면 오래된 이벤트가 버려질 수 있습니다.)

### 5.3 발신 UX (권장 흐름)

```
1) dry_run   → 대상 해석 확인(전송 없음, 레이트 리밋 미소비)
2) 사용자 확인 → send (accept_use_policy=true, client_request_id=새 UUID)
3) 결과 처리
   - result == "sent"        : 성공
   - result == "unconfirmed" : "전달 여부 미확인" → 자동 재시도 금지, 사용자에게 수동 확인 요청
   - error.code == "duplicate_request" : 이미 처리된 요청(성공). room/chars 무시
   - 502 window_not_open     : "카카오톡에서 해당 방을 열어 주세요" 안내
   - 409 target_ambiguous/ambiguous_room : 같은 이름 방/창이 여럿 → 사용자에게 정리 요청
```

- `client_request_id`는 **요청 1건당 새 UUID**를 쓰세요. 재사용하면 중복 전송은 막히지만
  "새 전송"도 되지 않습니다.
- `draft`는 사람이 Enter를 눌러야 하는 **확인 단계용**입니다. 자동화 파이프라인에는 쓰지 마세요.

### 5.4 여러 인터페이스로 팬아웃

- 백엔드는 외부 네트워크로 나가지 않습니다. **Slack/Telegram 전송은 클라이언트가** 합니다.
- 두 가지 소비 방식 중 하나를 고르세요.
  - **SSE**: 실시간성 좋음. 단일 소비자·재연결/갭 감지 로직 필요.
  - **Webhook**: 프로세스 분리·다중 소비자에 유리. 멱등 처리 + 유실 보정 필요.

### 5.5 최소 예제 (파이썬 표준 라이브러리만)

```python
import json, urllib.request

BASE = "http://127.0.0.1:8756"
TOKEN = "…"  # sesametok-backend token

def call(method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return res.status, json.loads(res.read() or b"{}")
    except urllib.error.HTTPError as exc:          # 오류도 JSON 본문을 읽는다
        return exc.code, json.loads(exc.read() or b"{}")

status, payload = call("GET", "/v1/status")
head = payload["archive"]["event_seq"]

status, rooms = call("GET", "/v1/rooms")
chat_id = rooms["rooms"][0]["chat_id"]

status, page = call("GET", f"/v1/rooms/{chat_id}/messages?limit=50")

status, result = call("POST", "/v1/messages", {
    "chat_id": chat_id, "text": ".......", "mode": "dry_run",
})
if result.get("result") != "dry_run":
    print("대상 해석 실패:", result.get("error"))

# SSE는 별도 스레드/프로세스에서: BASE + f"/v1/events?since={head}"
```

---

## 6. 테스트·검증 방법

### 6.1 이 저장소의 테스트

```bash
./.venv/bin/python -m pytest -q                       # 전체(현재 400개 통과)
./.venv/bin/python -m pytest -q tests/test_api_contract.py
./.venv/bin/python -m compileall -q src tests
```

**안전장치(반드시 유지)**: `tests/conftest.py`의 `env` 픽스처가 `SESAMETOK_DATA_DIR`,
`SESAMETOK_LOG_DIR`, `SESAMETOK_LAUNCHD_PLIST`, `SESAMETOK_APP_BUNDLE`을 `tmp_path`로 돌리고
`SESAMETOK_API=0`을 설정합니다. 테스트는 **실제 카카오톡 DB, 키체인, launchctl**에 접근하지
않습니다. 테스트 데이터는 전부 합성입니다(`tests/support.py`).

### 6.2 클라이언트 프로젝트가 할 검증

1. **계약 테스트(백엔드 없이)**: `MessageSender` 인터페이스(`send_text`)를 가짜로 구현해
   `SendQueue`를 테스트하세요(`tests/test_send_queue.py` 패턴).
2. **API 계약 테스트**: 임시 데이터 디렉터리로 백엔드를 띄우고 상태 코드·오류 코드를 고정하세요.
   `tests/test_api_contract.py`가 기대하는 값이 계약입니다.
3. **SSE 재개 테스트**: `409 event_replay_unavailable`은 **이벤트가 prune 된 뒤에만** 납니다.
   prune 전에는 `?since=0`이 전체 재생이므로, 409를 시험하려면 `oldest_event_seq`보다 작은
   `since`를 쓰거나 prune 이후 상태를 만들어야 합니다.
4. **webhook 서명 검증**: `X-Sesametok-Signature`의 HMAC을 **본문 원문 바이트**로 계산해 비교하고,
   `X-Sesametok-Event-Seq`로 멱등 처리하세요.
5. **발신 경로(전송 없이)** — `dry_run`만으로 다음을 확인할 수 있습니다.
   - 없는 `chat_id` → `404 room_not_found`
   - `accept_use_policy` 누락(`send`) → `400 no_consent`
   - `send`에 `text` 누락 → `400 bad_request`("text가 필요하다")
   - `limit=0` → `400 bad_request`
   - 같은 `client_request_id` 재요청 → 200 `duplicate_request`
6. **실제 발신 검증(마지막 단계)**: 사용자 승인 + **대상 방을 열어 둔 상태**에서, 본문은
   `.......` 같은 무해한 문자열로 **1건만**. `unconfirmed`면 **재시도하지 말고** 결과를 보고합니다.

### 6.3 현재 머신의 실측 상태(참고, 시점에 따라 변동)

| 항목 | 값 |
|---|---|
| 테스트 | **400 passed** |
| 실행 | launchd → `SesametokBackend.app` → python `foreground --managed` |
| 아카이브 | 방·메시지 수는 시점에 따라 증가(예: 411방 / 11,455메시지) |
| 권한 | 손쉬운 사용 = 백엔드 기준 `true` / 셸 기준 `false` (앱에만 귀속) |
| 읽기 | 전체 디스크 접근 **없이도** 동작(ADR-017) |
| 레이트 리밋 기본 | 60/분(전체) · 50/분(방별) |

### 6.4 실패 시 행동 표 (클라이언트 구현용)

| 상황 | 재시도? | 클라이언트 행동 |
|---|---|---|
| 200 `result=sent` | — | 성공 처리 |
| 200 `result=drafted` | — | 사람 확인 단계로 안내 |
| 200/409 `result=unconfirmed` | ❌ **절대 금지** | "전달 여부 미확인" 표시, 사용자 수동 확인 |
| 200 `duplicate_request` | — | 이미 처리됨. `room`/`chars` 무시 |
| 400 `no_consent` | — | UI 버그. 동의 플래그 누락 |
| 400 `bad_request` | — | 요청 수정(텍스트 길이/필수값) |
| 409 `target_ambiguous` / `ambiguous_room` | ❌ | 사용자에게 중복 창/방 정리 요청 |
| 429 `rate_limited` | ⏳ 시간 경과 후 | 지수 백오프(최소 1분) |
| 502 `window_not_open` | ❌ 즉시 | "해당 방을 카카오톡에서 열어 주세요" |
| 503 `not_running` / `accessibility_denied` | ⏳ 사용자 조치 후 | 카카오톡 실행/권한 안내 |
| 503 `send_unavailable` | ❌ | 백엔드 설정 문제(발신 비활성) |
| 500 `internal_error` | ⏳ 1회 | 로그 확인, 반복되면 보고 |
| 401 `unauthorized` | ❌ | 토큰 재확인(`sesametok-backend token`) |
| SSE `409 event_replay_unavailable` | — | REST로 히스토리 채우고 헤드에서 재구독 |
| SSE seq 갭 | — | `Last-Event-ID` 재접속 + REST 보정 |

---

## 7. 하면 안 되는 것

| 금지 | 이유 | 대신 |
|---|---|---|
| API를 `0.0.0.0`에 바인딩하거나 인터넷에 노출 | 로컬 개인 대화의 무단 노출 | `127.0.0.1`/UDS만. 터널링 금지 |
| 카카오톡 DB를 쓰기/수정 | 원본 손상 | 읽기 전용 API만 사용 |
| `unconfirmed` 결과를 자동 재시도 | 중복 전송 | 사용자 확인, 새 `client_request_id`로 새 요청 |
| `client_request_id` 재사용(다른 본문) | 이전 결과만 반환됨 | 요청마다 새 UUID |
| HTTP 상태만으로 발신 결과 판정 | `unconfirmed`가 200으로도 온다 | `result`·`error.code`로 판정 |
| 백엔드가 방을 열어 줄 것이라 가정 | **제거됨**(ADR-021) | 창이 없으면 `502 window_not_open` → 사용자 안내 |
| 이미지/파일 발신 기대 | **제거됨** | 텍스트만 |
| `draft`를 "전송 안 됨"으로 간주 | 구버전에서는 쓰기 자체가 전송일 수 있음 | `draft`는 사람 확인용으로만 |
| `chat_name`을 식별자로 사용 | UI 제목과 DB 이름이 다를 수 있음 | `chat_id` 사용 |
| `is_feed` 무시하고 `text`를 그대로 표시 | feed의 `text`는 원본 JSON일 수 있음 | `is_feed`/`is_placeholder` 확인 |
| `/health`로 발신 준비 판정 | 읽기 기준 + 기동 시점 스냅샷 | `/v1/status`의 `permissions`(역시 스냅샷) 또는 실제 발신 결과 |
| `retryable` 필드로 재시도 판단 | 429/`unconfirmed`도 `false` | 6.4절 표를 따를 것 |
| `since`에 `Z`/`+09:00` 사용 | 문자열 비교라 조용히 오답 | `...+00:00`(초 단위)만 |
| `before`에 다른 방의 `message_id` | 앵커를 못 찾아 **빈 페이지** | 같은 방의 id, 또는 `next_before` 사용 |
| SSE `seq`가 연속이라 가정 | 버스 큐(2000) 초과 시 유실 | 갭 감지 + 재구독 + REST 보정 |
| 이벤트만으로 초기 히스토리 확보 | 백필은 이벤트를 만들지 않음 | REST로 히스토리 채우기 |
| room name·닉네임·본문·토큰·키를 로그/커밋/문서에 남김 | 개인정보·비밀 유출 | 개수·id·상태·문자 수만 |
| 아카이브 키를 비대화형에서 출력 | 유출 위험 | `archive-key`는 사람이 직접 실행 |
| AppKit/AX를 백그라운드 스레드에서 호출 | 크래시 | 메인 스레드 전용 |
| `limit` 없이 전체 히스토리를 한 번에 | 메모리 폭증(최대 1000) | `limit` + `before` 페이지네이션 |
| 서버 폴링 주기를 클라이언트가 재시작으로 바꾸기 | 불필요한 중단 | 메뉴바 `동기화 주기` 또는 `SESAMETOK_POLL_SECONDS` |

---

## 8. 운영 체크리스트(처음 붙일 때)

```bash
# 1) 설치
./.venv/bin/pip install -e .
echo "export PATH=\"$PWD/bin:\$PATH\"" >> ~/.zshrc && source ~/.zshrc

# 2) 앱 패키징(권한을 앱 하나에만 주기 위해)
./.venv/bin/python packaging/build_app.py        # /Applications/SesametokBackend.app

# 3) 권한: 시스템 설정 > 개인정보 보호 및 보안 > 손쉬운 사용 → SesametokBackend 켜기
sesametok-backend permissions --accessibility

# 4) 로그인 시 자동시작 등록 + 기동
sesametok-backend register

# 5) 확인
sesametok-backend status          # 카카오톡 읽기 / 전체 디스크 접근 / 손쉬운 사용(권한 측정: 백엔드)
curl -s http://127.0.0.1:8756/health
```

- **전체 디스크 접근은 필요하지 않습니다**(ADR-017). 읽기가 실패하는 환경에서만 요구됩니다.
- 로그: `~/Library/Logs/sesametok-backend/{backend,out,err}.log`, `sesametok-backend logs -n 100`.

---

## 9. 트러블슈팅

| 증상 | 원인 | 해결 |
|---|---|---|
| `409 event_replay_unavailable` | 요청한 `since`가 prune 범위 밖 | REST로 히스토리 채우고 헤드에서 재구독 |
| SSE로 이벤트가 띄엄띄엄 | 버스 큐(2000) 초과로 유실 | seq 갭 감지 후 재구독 + REST 보정 |
| `502 window_not_open` | 대상 방 창이 닫혀 있음 | 사용자에게 방을 열어 달라고 안내(자동 열기 없음) |
| `503 not_running` | 카카오톡 미실행 | 카카오톡 실행 |
| `503 accessibility_denied` | 앱에 손쉬운 사용 권한 없음 | 시스템 설정에서 `SesametokBackend` 토글 ON |
| `503 send_unavailable` | 발신 워커 미구성 | 백엔드 기동 옵션(`SESAMETOK_API`) 확인 |
| `409 target_ambiguous` / `ambiguous_room` | 같은 이름 방/창이 여럿 | 사용자가 중복 창을 닫거나 방을 구분해야 함(클라이언트가 풀 수 없음) |
| 발신 `room`이 `null`, `chars`가 0 | `duplicate_request` | 이미 처리된 요청. 정상 |
| `status`가 `중지됨`인데 API는 응답 | 중단된 재시작으로 락 inode가 어긋난 드문 경우 | `stop` → `start` |
| `restart`가 "시작하지 않았습니다"인데 실제로 뜸 | 대기 로직 오탐(알려진 결함) | `status`로 실제 상태 확인 |

---

## 10. 이 프로젝트의 히스토리 (왜 지금 이 모양인가)

클라이언트 에이전트가 알아야 할 결정 타임라인입니다. 상세 근거는 `DECISIONS.md`.

1. **SQLCipher 바인딩**(ADR-002): `sqlcipher3==0.6.3` 고정. `sqlcipher3-binary`는 macOS arm64
   wheel이 없습니다.
2. **미디어 비목표**(ADR-003): 수신 미디어는 내려받지 않고, 미지원 타입은 고정 문구로 치환.
   메시지 타입의 `0x4000` 플래그를 마스킹하지 않으면 실제 텍스트가 치환됩니다.
3. **자체 아카이브 암호화**(ADR-004): 아카이브는 SQLCipher 전체 암호화, 키는 Keychain.
   카카오톡 DB 키(passphrase)와 우리 키(raw key literal) 형식이 다릅니다.
4. **loopback webhook**(ADR-005), **보존 30일/10만건(이벤트만)**(ADR-006).
5. **메뉴바 메뉴 개편**(ADR-007), **PyObjC + 표준 HTTP**(ADR-008), **개발/운영 경로 분리**(ADR-009).
6. **API 세부 의미 확정**(ADR-010): `/health` 토큰 예외, SSE 헤드 시작, `409` 재생 불가,
   webhook CRUD, 오류 상태 매핑, JSON 405.
7. **발신 워커가 두 번째 writer**(ADR-011): WAL + `busy_timeout=5000`.
8. **발신 파이프라인 구현 상태**(ADR-012): UI 단계별 설계와 당시 미검증 항목.
9. **테스트 안전장치**(ADR-013), **커튼 키보드 우선 취소**(ADR-014, 현재 커튼은 미사용 잔존).
10. **TCC 권한을 앱 번들에 귀속**(ADR-015) — 실측: 앱이 띄운 자식만 권한을 물려받고, 같은
    인터프리터를 셸에서 쓰면 권한 0. Finder/`open`/launchd 모두 동일.
11. **`전체 디스크 접근` 오표기 수정**(ADR-016) → **권한 자체가 불필요**(ADR-017).
12. **수명주기 결함 수정**(ADR-018): 역추적 Pool 워커의 SIGTERM 상속으로 `join` 정지,
    `bootout` 후 python 고아, `status` 권한 출처 혼동.
13. **자동 방 열기 시도 → 철회**(ADR-019 → **ADR-021**): 창 열기는 최전면을 요구해 포커스를
    빼앗고 목록 클릭은 40초 안팎 걸렸습니다. **지금은 창이 없으면 502로 거부**합니다.
14. **메뉴바 폴링 주기**(ADR-020): 2/5/10/30/60초, `<data_dir>/preferences.json`에 영속화.
15. **이미지 발송 철회**: 인터페이스별 업로드 방식 차이 + outbox 중복 저장. 관련 코드·테스트·
    문서를 **삭제**했습니다(새 ADR 없이 히스토리에서 제거).
16. **발신 레이트 리밋 기본값 변경**: 6/3 → **60/50**(커밋 `0f29900`).

**제거된 것을 기억하세요**: 자동 방 열기, 이미지/파일 발신, `image_path` API 필드, outbox 디렉터리,
커튼 기반 취소(코드는 잔존하나 미사용). `HANDOFF_MESSAGING.md`는 이 제거 이전의 역사 문서이며
outbox·이미지 설계를 여전히 설명하므로 **참고하지 마세요**.

---

## 11. 용어

| 용어 | 의미 |
|---|---|
| 아카이브 | 이 백엔드가 만든 SQLCipher 암호화 DB(`archive.sqlite3`). 카카오톡 DB와 별개 |
| 백필 | 최초 기동 시 30일/10만건까지 과거 메시지를 채우는 작업(이벤트를 만들지 않음) |
| 폴링 | 주기적으로 카카오톡 DB 스냅샷을 떠 diff를 만드는 것 |
| fingerprint diff | 메시지 필드 지문으로 `created`/`updated`를 가르는 순수 로직 |
| 대상 해석(`resolve_target`) | `chat_id` → (방 이름, 같은 이름 내 최신순 순위)로 환원 |
| `unconfirmed` | 전송됐을 수도, 안 됐을 수도 있는 상태. 재시도 금지 |
| 커튼 | 과거 초안 붙여넣기 시 입력을 잠시 삼키던 장치. **현재 미사용(잔존 코드)** |

---

## 12. 참고

- `README.md` — 설치·사용·환경변수·API 요약
- `DECISIONS.md` — ADR 전체(002~021)와 부록(실DB 사실, 라이브 API 검증, 메뉴바 검증, M9 실기 검증)
- `AGENTS.md` — 저장소 작업 규칙(개인정보·발신 안전·스레드 규칙)
- `src/sesametok_backend/api/routes*.py` — 계약의 실제 구현
- `tests/test_api_contract.py` — 응답 형태·오류 코드의 기준
