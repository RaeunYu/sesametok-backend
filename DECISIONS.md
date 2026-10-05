# DECISIONS — 확정 사항 기록 (ADR)

이 문서는 이 프로젝트에서 내린 **되돌리기 어렵거나 이후 작업의 전제가 되는 결정**과 그 근거를 기록한다.
"왜 이렇게 되어 있는가"를 파악하려는 사람과 에이전트의 단일 출처다.

- 작성 시점의 환경: macOS 26.3 (arm64), Python 3.13.3 (pyenv), KakaoTalk.app 설치·실행 중, Homebrew 존재.
- 상태 표기: **Accepted**(확정) / **Superseded**(대체됨).
- 번호는 안정적인 식별자다. 폐기된 항목이 생겨도 결번을 채우지 않는다.

---

## ADR-002. SQLCipher Python 바인딩 — `sqlcipher3==0.6.3` (Accepted)

**결정.** 의존성을 `sqlcipher3==0.6.3`으로 고정한다. `sqlcipher3-binary`는 사용하지 않는다.

**근거(실측).**
- `sqlcipher3-binary`는 PyPI에 **manylinux x86_64 wheel만** 게시한다(0.6.0 기준). macOS wheel도, arm64 wheel도 없다 → 이 머신에서 설치 불가.
- `sqlcipher3` 0.6.3은 `cp313-macosx_11_0_arm64` wheel을 제공한다. 설치·임포트 성공.
- 검증: `PRAGMA cipher_version` → `4.12.0 community`, `sqlite_version` → `3.51.1`. raw key(`PRAGMA key = "x'<64hex>'"`) + `PRAGMA cipher_compatibility = 3` 순서로 생성→재오픈 성공, 오답 키는 `file is not a database`로 거부, 파일 헤더가 평문 SQLite가 아님을 확인.
- Homebrew `sqlcipher`나 소스 빌드가 필요 없다 → 설치 승격도 불필요.

**영향.** bootstrap 명령은 이 패키지를 쓴다. Python은 3.13.3 사용(휠 태그가 cp313).

---

## ADR-003. 미디어·이모티콘은 비목표, 미지원 타입은 텍스트로 치환 (Accepted)

**결정.**
1. 미디어(이미지/동영상/파일)와 이모티콘은 **다운로드하지도, 로컬 복사하지도, 별도로 폴링하지도 않는다.** 메뉴의 "미디어 폴링 여부", "이모티콘 폴링 여부" 토글은 **삭제**한다.
2. 저장소의 `attachment`/`supplement` 원본 payload는 아카이브에 저장하지 않는다(프라이버시·용량).
3. 텍스트(`type_1`)와 피드(`type_0`) 외의 메시지는 **일반 텍스트 메시지로 치환해 저장**한다.
   - `message_type = "text"`
   - `original_message_type = "<원본 type_<n>>"` 보존
   - `is_placeholder = true`
   - `text = "[지원하지 않는 메시지 타입입니다. 원본 메시지를 확인해주세요.]"` (고정 문구)
4. 실제 `type_<n>` 값 매핑과, 미지원 행의 `message` 본문 공백 여부는 구현 중 실DB에서 확인해 확정한다.

**영향.** 아카이브 스키마에 `original_message_type TEXT`, `is_placeholder INTEGER NOT NULL DEFAULT 0` 두 컬럼을 추가한다(ADR-004). Message DTO에도 같은 필드를 노출한다. "본문이 비어 있지 않은 행만 읽는다"는 단순 필터는 미지원 타입 행을 놓칠 수 있으므로, 타입 기반으로 확장한다.

**근거.** 미디어/이모티콘은 비목표이고 "저장"은 로컬 파일 복사이지만 폴링·다운로드는 하지 않기로 확정했다. 인터페이스가 렌더링할 수 없는 타입이 조용히 사라지면 대화 맥락이 끊기므로 자리표시자를 남긴다.

---

## ADR-004. 아카이브는 SQLCipher 전체 파일 암호화, 키는 Keychain (Accepted)

**결정.**
- 아카이브(`<data_dir>/archive.sqlite3`)는 **SQLCipher로 전체 파일 암호화**한다. 평문 SQLite를 쓰지 않는다.
- 키는 최초 실행 시 CSPRNG로 32바이트 생성해 **macOS 로그인 Keychain**에 `service=com.sesametok.sesametok-backend`, `account=archive`로 저장한다. SQLCipher에는 `PRAGMA key = "x'<64hex>'"`(raw key)로 전달한다.
- 폴백: 환경변수 `SESAMETOK_ARCHIVE_KEY`(64자 hex)가 Keychain보다 우선. 테스트/CI/헤드리스 전용.
- **키 파일은 만들지 않는다. 평문 폴백도 없다(fail-closed).** Keychain 접근 실패 + env 미설정이면 명확한 오류로 기동을 중단한다.
- 최초 실행 시(대화형 CLI) 생성된 키를 화면에 1회 표시하고, 사용자가 **별도로 안전하게 보관**하도록 안내한다. 로그·문서·테스트·커밋에는 키를 남기지 않는다. 재표시는 전용 명령(TTY 전용)으로만 가능하다.
- 키 분실 시 아카이브는 폐기하고 카카오톡 원본 DB에서 재적재한다(원본 DB는 불변).

**근거.** 전체 파일 암호화이므로 대화 본문뿐 아니라 방 이름·닉네임도 함께 보호된다. Keychain add/read/delete는 승격 실행에서 정상 동작함을 실측했다(샌드박스에서는 거부).

---

## ADR-005. Webhook push 추가 — loopback 전용 (Accepted)

**결정.** SSE에 더해 webhook push를 제공한다.

- 엔드포인트: `POST /v1/webhooks`(secret 1회 반환) / `GET /v1/webhooks`(secret 비노출) / `PATCH /v1/webhooks/{id}`(active 토글) / `DELETE /v1/webhooks/{id}`.
- **대상 URL은 loopback(`127.0.0.1`, `::1`, `localhost`)만 허용**하고 스킴은 `http`만 허용한다. 외부 URL은 등록 시 거부한다. 외부 메신저로의 전달은 인터페이스 프로젝트의 책임이다.
- 전달 보장: at-least-once, 지수 백오프 재시도(최대 5회), `X-Sesametok-Signature: sha256=<HMAC-SHA256(secret, body)>`, `X-Sesametok-Event-Seq`, 구독별 `cursor_seq`로 재개, 실패 시 `last_error` 기록.
- secret은 아카이브에 암호화 저장한다.
- UDS(`<data_dir>/backend.sock`, 0600)도 SSE·webhook 양쪽을 지원한다.

**근거.** API는 로컬 전용이고 인터넷에 노출하지 않는다. 인터페이스가 어떤 메신저를 쓸지 정해지지 않았으므로, 백엔드는 로컬로만 push하고 외부 전달은 인터페이스가 맡는 구조가 맞다.

---

## ADR-006. 보존 정책 — 30일 또는 100,000건 (Accepted)

**결정.** 이벤트 보존과 최초 백필 모두 **30일 또는 100,000건 중 먼저 도달하는 쪽**까지로 한다. prune은 기동 시 + 1일 1회.

- 이벤트 보존: `SESAMETOK_EVENT_RETENTION_DAYS=30`, `SESAMETOK_EVENT_RETENTION_MAX=100000`.
- 최초 백필 상한: `SESAMETOK_BACKFILL_DAYS=30`, `SESAMETOK_BACKFILL_MAX=100000`.
- 보존 범위를 벗어난 `Last-Event-ID`/`?since=`로 SSE 재개를 요청하면 명확한 오류를 반환하고, 클라이언트가 `/v1/rooms/{chat_id}/messages`로 백필하도록 안내한다.

**대체.** 이 항목은 이전 값(7일/10,000건)을 대체한다.

---

## ADR-007. 메뉴바 메뉴 구성 변경 (Accepted)

**결정.** 메뉴에서 두 항목을 **삭제**하고 한 항목을 **추가**한다.

- 삭제: "미디어 폴링 여부", "이모티콘 폴링 여부" (ADR-003).
- 추가: **webhook 상태**(활성 구독 수, 마지막 성공 시각) 읽기 전용.
- 유지: 상태 요약, 카카오톡, 권한, 지금 동기화, 일시정지/재개, API 토큰 복사, 로그 열기, 로그인 시 자동시작, 백엔드 종료.
- 드롭다운에 미지원 타입이 치환 저장된다는 설명과 저장공간 관련 경고를 유지한다.

---

## ADR-008. 기술 스택 — 순수 PyObjC + 표준 HTTP 서버 (Accepted)

**결정.**
- 메뉴바/커튼: **순수 PyObjC**(AppKit/Quartz/ApplicationServices). `rumps` 미사용.
- API 서버: 표준 라이브러리 `http.server.ThreadingHTTPServer`. FastAPI/uvicorn 미사용.
- 테스트: `pytest`.

**근거.** 커튼은 non-activating NSPanel·`CGEventTap` 등 AppKit 저수준 제어를 요구하므로 `rumps`로는 결국 PyObjC를 섞게 된다. loopback 전용 API에 ASGI 스택은 의존성과 TCC 표면만 늘린다. 실측 결과 pyobjc 스택과 pytest는 Python 3.13.3에서 정상 설치·임포트된다.

---

## ADR-009. 개발/운영 경로 전략 (Accepted)

**결정.**
- 개발용 venv는 저장소 내부 `./.venv`, 개발용 데이터는 `./.devdata`.
- 런타임 경로(데이터·로그·아카이브·outbox·소켓)는 전부 환경변수로 재정의 가능하게 한다.
- 운영은 표준 macOS 경로(`~/Library/Application Support/sesametok-backend`, `~/Library/Logs/sesametok-backend`)를 기본값으로 쓴다.
- LaunchAgent 등록과 실운영(launchd) 검증은 저장소 밖 쓰기가 필요하므로 그 시점에만 승격을 요청한다.

**근거.** 개발 에이전트 셸은 워크스페이스 밖 쓰기가 차단되어 있어(`~/.local/share` 쓰기 `Operation not permitted`) 자율 진행이 막힌다. TCC는 실행 파일 절대경로에 귀속되므로 `./.venv` 경로가 고정되어 있으면 개발 중에도 유효하다.

---

## ADR-010. 공개 API의 세부 의미 확정 (Accepted)

엔드포인트는 아래와 같이 확정한다.

1. **`GET /health`는 토큰 없이 허용**한다. 감독 프로세스가 비밀 없이 생존을 확인할 수 있어야 하기 때문이다.
   나머지 `/v1/*`는 전부 `Authorization: Bearer <token>`이 필요하다.
2. **SSE 재개 의미**: `?since=`/`Last-Event-ID`가 있으면 그 이후를 아카이브에서 재생한다.
   **없으면 현재 head부터** 스트리밍한다(새 인터페이스가 100k 이벤트를 재생하지 않도록).
3. **보존 범위를 벗어난 재개 요청**은 SSE 헤더를 보내기 전에 `409 event_replay_unavailable`로 거부하고
   `/v1/rooms/{chat_id}/messages` 백필을 안내한다.
4. **webhook 구독 API**를 추가한다(ADR-005). secret은 생성 응답에서 1회만 노출하고 목록에서는 숨긴다.
5. **발신 오류의 HTTP 상태 매핑**: 검증/동의 400, 방 없음 404, 모호/미확정 409, 레이트 리밋 429,
   권한·미실행 503, 그 밖의 UI 실패 502.
6. `PUT` 등 미지원 메서드도 표준 501 HTML이 아니라 **JSON `405 method_not_allowed`**로 응답한다.

---

## ADR-011. 발신 워커는 아카이브의 두 번째 writer다 (Accepted)

메시지·방·이벤트(폴링분)는 폴러가 쓰지만, `send_requests`와 `send.result` 이벤트는 발신 워커가 기록해야 한다. 따라서:

- 각 스레드는 **자신의 SQLCipher 연결**을 연다(SQLite 연결은 스레드 친화적).
- WAL + `busy_timeout = 5000`으로 폴러와 발신 워커의 쓰기를 직렬화한다. 쓰기는 밀리초 단위다.
- **메시지·방·이벤트(폴링분)는 폴러만**, **발신 기록은 발신 워커만** 쓴다. 교차 쓰기는 없다.

또한 발신 API의 `image_path`는 **`<data_dir>/outbox` 내부의 기존 파일만** 허용한다(경로 탈출 차단).
인터페이스가 이미지를 올려 outbox에 두고 그 절대 경로를 넘기는 것이 계약이다.

---

## ADR-012. 발신 파이프라인 구현 상태 (Accepted, live 미검증)

`send/accessibility.py`는 발신 파이프라인의 **순서·관찰 지점·오류 코드·타이밍 상수**를 구현한다:
권한 확인 → `pgrep` → 창 해석(모호하면 거부) → 입력창(`AXScrollArea > AXTextArea`) → (background-only면
비어 있음 확인) → `AXValue` 쓰기 → Enter → Send 버튼 → 가시 경로(idle 대기 → 활성화 → raise → 전역 Enter
→ 실제 클릭). 성공 판정은 **입력창 `AXValue`가 비워졌는가**이다. 초안은 `AXValue`를 쓰지 않고
클립보드+붙여넣기 후 값 일치를 확인한다.

**미검증/미구현**
- 라이브 검증은 손쉬운 사용 권한과 사용자가 지정한 테스트 방이 필요하다(현재 `AXIsProcessTrusted=false`).
- 입력 차단 커튼은 구현했다(ADR-014). 다만 화면 표시·이벤트 탭 동작은 라이브 미검증이다.
- 검색/채팅목록 기반 방 열기는 단순화된 형태로 구현했다(6회 재시도, `attempt % 3` 전략 순환,
  최근 60행, 클릭 직전 행 재확인). "메인 창을 표본 10행으로 판별"하는 휴리스틱은 적용하지 않았다.
- 이미지 전송 확인 시트 탐색은 depth 4 시트 / depth 6 버튼 규칙을 근사했다.

이 항목들은 발신·커튼의 계약을 만족시키는 방향으로 후속 작업에서 보강한다.

---

## ADR-013. 테스트 안전장치 (Accepted)

- `tests/conftest.py`의 `env` 픽스처가 `SESAMETOK_DATA_DIR`/`LOG_DIR`/`LAUNCHD_PLIST`를 모두
  `tmp_path`로 돌린다. 테스트는 실제 키체인·launchd·카카오톡 DB에 접근하지 않는다.
- `SESAMETOK_LAUNCHD_PLIST` override는 실제 `~/Library/LaunchAgents` 밖 경로를 허용하되,
  CLI는 명시적 옵트인(`SESAMETOK_ALLOW_PLIST_OVERRIDE=1`) 없이는 임시 경로 밖 쓰기를 거부한다.
- `SESAMETOK_UDS_PATH` override가 있다. macOS의 AF_UNIX 경로 길이 제한(~104바이트) 때문에
  깊은 데이터 디렉터리에서 필요하다. 경로가 100바이트를 넘으면 `ApiService`가 UDS를 **건너뛰고
  경고 로그**를 남긴다(HTTP는 계속 동작).
- `SESAMETOK_API`(기본 on)로 API·webhook·발신 워커 기동을 끌 수 있다. 단위 테스트는 `env` 픽스처에서
  이를 `0`으로 두어 소켓을 바인딩하지 않는다.

---

## ADR-014. 입력 차단 커튼의 취소는 키보드 우선 (Accepted)

커튼 규정은 "취소: Esc 또는 취소 버튼 클릭"이라고 적는 한편, 같은 창에 대해 **마우스 무시**를 요구한다.
둘은 양립하지 않는다(입력을 삼키는 탭이 클릭도 삼키므로 버튼이 눌리지 않는다). 따라서:

- 커튼 창은 `setIgnoresMouseEvents_(True)`를 유지하고, **취소는 Esc 키다운으로만** 처리한다.
- 입력 차단 중에는 사용자의 모든 키/마우스 이벤트를 **삼키되**, Esc만 `cancelled`를 세운다.
  (초기 구현이 아무 입력에나 취소를 걸어 마우스만 움직여도 취소되던 버그를 수정했다.)
- 우리가 합성한 이벤트는 `kCGEventSourceUserData`에 태그를 심어 통과시킨다.
- 커튼이 표시되지 않으면(ack 실패·탭 설치 실패) **비차단으로 진행하되 frontmost 검증은 유지**한다.
  보이지 않는 차단은 만들지 않는다.
- `background_only=True`에서는 이미지 전송과 초안 남기기를 **거부**한다(`background_only_unsupported`).
  둘 다 전역 Cmd+V가 필요하고, 이는 앱을 앞으로 가져오는 동작이기 때문이다.

---

## 운영 체크포인트

- 손쉬운 사용 권한: `AXIsProcessTrusted()`가 현재 `False`. 발신 검증 전 권한 부여 필요.
- 실제 전송은 사용자 승인 + 사용자가 지정한 방에서만, `dry_run` → `draft` → `send` 순으로.

---

## 부록 A. 실DB 검증으로 확정된 사실

실제 KakaoTalk DB(284.8MB, 활성 WAL, 테이블 19개)로 읽기 경로를 돌려 확인한 내용이다.
개인정보는 출력하지 않았고 개수·타입·상태만 확인했다.

### A-1. `secure_key`는 **passphrase 문자열**로 전달한다

`PRAGMA key = '<256-hex secure_key>'` — 256자 hex를 **문자열 그대로** SQLCipher에 passphrase로 준다.
`PRAGMA key = "x'<hex>'"`(raw key literal)로 넘기면 SQLCipher가 128바이트 raw key로 해석해
`file is not a database`로 거부된다. 구현 초기 실수였고 `kakao/cipher.py`에서 수정했다.
합성 DB 픽스처도 동일한 passphrase 규약으로 생성하도록 바꿔 이 차이를 테스트가 잡도록 했다.

### A-2. 계정 식별자는 SHA-512 역추적이 실제 성공 경로

- 이 환경의 preference plist에는 직접 `userId`/`user_id` 키가 **없다**. `AlertKakaoIDsList`에서 나온 후보 4개는
  모두 활성 계정 해시와 불일치했고, `~/.cache/k-skill/kakaotalk-mac-auth.json`도 없었다.
- `DESIGNATEDFRIENDSREVISION:<128-hex>` 활성 계정 해시 1개에 대해 `sha512(str(user_id))` 역추적을 수행해
  **user_id를 찾았다**(10코어, 21.2초). 그 user_id로 계산한 `database_name`이 실제 파일명과 일치했다.
- 따라서 `resolve_identity(..., allow_reverse_lookup=True)`가 첫 실행의 실제 경로다. 발견 즉시 캐시에 저장한다.
- 처리량 실측: 초당 약 179만 해시/코어. 1e9 탐색 ≈ 56초(10코어).
- 역추적은 **앞선 저렴한 후보가 모두 실패했을 때만** 실행된다(캐시가 있으면 약 0.2초에 끝난다).

### A-3. 오픈채팅 연결은 `NTChatRoom.linkId` → `NTOpenLink.linkId`

`NTOpenLink`에는 `chatId` 컬럼이 **없다**(컬럼: `linkId`, `linkName`, `userId`, `active`, ...).
오픈채팅 방은 `NTChatRoom.linkId`가 채워진 행이며, 실측 412개 방 중 **3개**가 여기 해당하고
3개 모두 `NTOpenLink`와 조인됐다. `reader.open_chat_ids()`를 `linkId` 기준으로 수정했다.

### A-4. 메시지 타입은 `0x4000` 플래그를 마스킹한 뒤 판정한다

실제 타입 분포에 `16385`, `16386`, `16387`, `16396`, … 이 있었고, 이는 `0x4000 | 1`, `0x4000 | 2` … 로
**상위 비트는 플래그, 하위 14비트가 실제 타입**임을 확인했다. 마스킹 없이는 플래그가 붙은 텍스트(16385, 전체 252건)가
placeholder로 덮여 실제 본문이 소실된다. `reader.TYPE_FLAG_MASK = 0x3FFF`로 base 타입을 구하고,
placeholder로 치환할 때는 원본 raw 값을 `original_message_type`에 보존한다.

### A-5. 실측 규모 (참고)

| 항목 | 값 |
|---|---|
| DB 크기 / WAL | 284.8MB / 1.55MB(활성) |
| 방 | 412 (direct 378 / group 34) → 오픈채팅 3 제외 시 **409** |
| 메시지 | 664,297건 (본문 공백 29,090건) |
| 타입 1(텍스트) | 597,599건 |
| 7일 창 리더 결과 | 2,265건, 디코딩 실패 0, placeholder 281, feed 9, 답장 28 |
| 방 이름 미해석(`chat-<id>`) | 3개 |

---

## 부록 B. 최종 라이브 검증 기록

실제 카카오톡 DB + 실제 HTTP 서버로 확인한 결과(개인정보는 출력하지 않고 개수·상태·필드명만 확인).

| 항목 | 결과 |
|---|---|
| `GET /health` | 200 · `ok=true` · `kakaotalk_running=true` · `permissions_ok=false`(손쉬운 사용 미부여) · `polling=true` |
| `GET /v1/rooms` | 200 · 방 410개 · 필드 `chat_id/chat_name/chat_type/last_message_at/message_count` |
| `GET /v1/rooms/{chat_id}` | 200 · 단건 필드 일치 |
| `GET /v1/rooms/{chat_id}/messages?limit=5` | 200 · 5건 · `has_more=true` · DTO 13개 필드 전부 존재 |
| `GET /v1/messages/{message_id}` | 200 · 동일 id 반환 |
| `GET /v1/status` | 200 · 아카이브 방 410 / 메시지 2,245 / `event_seq=0` |
| `POST /v1/sync` | 202 · `{"result":"scheduled"}` |
| 없는 방 조회 | 404 · `room_not_found` |
| 외부 webhook URL 등록 | 400 · loopback만 허용 |
| `GET /v1/events` | 200 · `Content-Type: text/event-stream` |
| 최초 백필(7일 상한) | 2,245건 / 410방 / **이벤트 0건**(백필은 이벤트를 발행하지 않는다) |
| 기동 후 SIGTERM | **exit 0** · 락 파일 정리 · 순서대로 종료 |

미검증: SSE로 **실시간 이벤트 전달**은 실제 수신 메시지가 도착해야 확인 가능하다.
실제 전송(`mode=send`)은 손쉬운 사용 권한과 사용자 지정 방이 필요해 수행하지 않았다.

---

## 부록 C. 메뉴바 실기 검증 (실제 AppKit 프로세스)

실제 AppKit 프로세스에서 `NSStatusItem`/`NSMenu`를 생성해 확인했다(개인정보는 출력하지 않음).

| 확인 | 결과 |
|---|---|
| `NSStatusItem` / `NSMenu` 생성 | 성공 (메뉴 18개 항목) |
| 아이콘 | `NSImage` 존재 · `isTemplate=true` (다크/라이트 자동 적응) |
| 상태 전이 | 전체 디스크 접근 정상 + 손쉬운 사용 미부여 → **`성능저하`(degraded)** |
| 메뉴 구성 | 카카오톡 · 권한(2종) · 지금 동기화 · 일시정지 · API 토큰 복사 · 로그 열기 · 로그인 시 자동시작 · webhook · 종료 + 설명·경고 2줄 |
| 미디어·이모티콘 **폴링 토글** | **없음** (ADR-003/007 준수, 설명 문구만 존재) |
| webhook 항목 | 읽기 전용 · 구독 수/마지막 성공만 (URL·secret 없음) |
| 자동시작 체크박스 | `autostart_provider`가 True일 때 체크됨 (launcher 연동 확인) |
| 비-메인스레드 `stop()` 마샬링 | 예외 없이 반영 · `_stop_event` 설정 |
| refresh 스레드 | 기동·정지 정상 |
| 메뉴/툴팁 개인정보 | 없음 (본문·방 이름 미포함) |

**주의(환경 한계)**: 이 환경에서 `AppHelper.runEventLoop(main=...)`은 앱 런치 알림이 없어
`main` 콜백이 호출되지 않았다. 그래서 런 루프 진입 대신 컨트롤러를 직접 구동해 검증했고,
`run_foreground`의 **메뉴바 포함 실기동**은 여전히 육안 확인이 필요하다(라이브 검증은
`SESAMETOK_NO_MENUBAR=1` 경로로 수행했다). `_menu_quit`의 launchd 해제 경로는 실 launchd 대신
스텁 테스트로 고정했다(`tests/test_runtime_autostart.py`).
