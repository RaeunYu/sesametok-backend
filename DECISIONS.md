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
- 런타임 경로(데이터·로그·아카이브·소켓)는 전부 환경변수로 재정의 가능하게 한다.
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
- `background_only=True`에서는 초안 남기기를 **거부**한다(`background_only_unsupported`).
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

---

## ADR-015. 권한은 앱 번들 하나에 귀속시킨다 (Accepted, 실측 검증)

**문제.** 권한 안내가 공용 pyenv 인터프리터(`~/.pyenv/versions/3.13.3/bin/python3.13`)를 가리켰다.
그러면 **같은 인터프리터를 쓰는 모든 프로젝트가 전체 디스크 접근·손쉬운 사용을 함께 얻고**, 경로가
`.pyenv` 숨김 폴더라 시스템 설정의 `+`로 추가하기도 어렵다.

**결정.** `SesametokBackend.app`(얇은 스텁, `LSUIElement`)을 만들어 권한 대상으로 삼는다.
스텁은 파이썬을 **번들하지 않고** `Contents/Resources/target`에 적힌 인터프리터를 **자식으로
spawn**하며(exec 금지), 자신은 부모로 남는다. LaunchAgent의 `ProgramArguments`도 이 앱을 가리킨다.

**실측 근거.** 동일한 `/Users/.../.venv/bin/python`을 여러 컨텍스트에서 실행한 결과:

| 실행 경로 | FDA 게이트 경로 | `AXIsProcessTrusted()` |
|---|---|---|
| Finder 더블클릭 | 4/4 허용 | True |
| `open` (LaunchServices) | 4/4 허용 | True |
| launchd (LaunchAgent) | 4/4 허용 | True |
| 셸에서 같은 python | 0/4 | False |
| 공용 pyenv python 직접 | 0/4 | — |

FDA 게이트 경로는 `~/Library/Safari`, `~/Library/Mail`, `~/Library/Messages`,
`~/Library/Containers/com.apple.Safari`다. 즉 **앱에 준 권한이 그 앱이 띄운 자식에게만 적용**되고,
공용 인터프리터는 영향을 받지 않는다. 실행 방식(Finder/`open`/launchd)은 결과에 영향이 없었고,
유일한 변수는 시스템 설정의 **토글 ON/OFF**였다 — 목록에 보이는 것만으로는 부족하다.

**구현.** `packaging/stub.c` + `packaging/Info.plist.tmpl` + `packaging/build_app.py`.
앱이 없으면 이전과 같이 인터프리터를 대상으로 삼는다(하위 호환). 앱을 다시 빌드하면 cdhash가
바뀌어 부여된 권한이 무효가 되므로, 경로를 고정하고 재빌드 후 다시 부여하도록 안내한다.

**동작.** 인자 없이 실행(더블클릭)하면 CLI `start`와 같은 일을 한다: plist가 없으면 등록하고
launchd로 기동하며, 이미 실행 중이면 아무것도 하지 않는다. `--managed`는 LaunchAgent가 넘긴다.

## ADR-016. `전체 디스크 접근: 정상` 오표기 수정 (Accepted)

**문제.** `full_disk_access_ok()`는 **카카오톡 저장소를 나열할 수 있는가**만 검사했다. 이 머신에서
카카오톡 컨테이너는 TCC 보호 대상이 아니라 **전체 디스크 접근 권한 없이도 읽혔고**, 그래서 FDA가
0개인데도 `status`가 `전체 디스크 접근: 정상`이라고 표시했다. 게다가 필요하지도 않은 권한을
사용자에게 요구했다.

**결정.** 두 개념을 분리한다.

- `store_readable(roots)` — 제품이 실제로 필요로 하는 조건("지금 읽을 수 있는가").
- `full_disk_access_granted()` — TCC가 보호하는 경로(`~/Library/Safari` 등)를 읽어 판정한 **실제
  권한 보유 여부**. 그런 경로가 없으면 `None`(판정 불가). **카카오톡 저장소는 판정에 쓰지 않는다.**

**영향.**
- `collect()`가 `store_readable`과 `full_disk_access`를 함께 보고하고 `full_disk_access_ok`는 제거됐다.
- `permission_guidance()`는 **읽기가 실패할 때만** 전체 디스크 접근을 요구한다. 판정만 되고 읽히면
  아무것도 요구하지 않는다(정보 제공도 하지 않는다).
- `/health`의 `permissions_ok`는 `store_readable`을 뜻하게 됐고, `store_readable`·`full_disk_access`·
  `accessibility_trusted`가 함께 실린다.
- 메뉴바의 권한 줄은 `전체 디스크 접근:` → **`카카오톡 읽기:`**(실제 접근 가능성)로 바뀌었고,
  `status`는 `카카오톡 읽기: 가능|불가`와 `전체 디스크 접근: 부여됨|부여 안 됨|알 수 없음`을 따로 보여준다.

---

## ADR-017. 전체 디스크 접근 권한은 필요하지 않다 (Accepted)

**정정.** ~~이 프로젝트는 전체 디스크 접근 권한이 카카오톡 DB 읽기에 필요하다.~~ **필요하지 않다.**
실제로 필요한 권한은 **손쉬운 사용 하나**뿐이며, 그것도 발신에만 필요하다.

**이유(실측).** 카카오톡 컨테이너
(`~/Library/Containers/com.kakao.KakaoTalkMac/Data/Library/Application Support/com.kakao.KakaoTalkMac`)는
**TCC 보호 대상이 아니다.** 같은 사용자로 도는 프로세스는 전체 디스크 접근 권한 없이 디렉터리를
나열하고 284.8MB DB를 읽을 수 있었다. 대조군으로 `~/Library/Safari`, `~/Library/Mail`,
`~/Library/Messages`, `~/Library/Containers/com.apple.Safari`는 같은 조건에서
`Operation not permitted`였다 — TCC는 "샌드박스 컨테이너"라는 이유가 아니라 **경로별로** 보호한다.
Apple 자체 앱 컨테이너는 보호되지만 제3자 앱 컨테이너는 그렇지 않다.

**운영.**
- 안내는 **손쉬운 사용**을 기본으로 하고, 전체 디스크 접근은 `store_readable == False`일 때만 요구한다.
- `status`는 `카카오톡 읽기: 가능|불가`와 `전체 디스크 접근: 부여됨|부여 안 됨|알 수 없음`을 따로
  보여준다. 뒤쪽은 TCC 보호 경로로 판정한 **실제 권한 보유 여부**이며, 필요성과는 별개다.
- 이 문서를 포함한 모든 문서에서 "전체 디스크 접근이 필요하다"는 서술은 취소선으로 정정한다.

---

## ADR-018. 앱 수명주기 결함 2건과 `status` 권한 출처 수정 (Accepted)

앱을 launchd로 실제 기동하면서 드러난 결함이다. 셋 다 실측으로 재현하고 고쳤다.

### (1) 역추적 워커가 부모의 SIGTERM 핸들러를 물려받아 `join()`이 멈춘다

`run_foreground`는 시작 단계의 긴 작업(역추적)을 인터럽트할 수 있도록 **Pool 생성 전에**
SIGTERM 핸들러를 설치한다. `fork`로 만들어진 워커가 그 핸들러를 상속하면
`Pool.terminate()`의 SIGTERM을 **삼켜 죽지 않고**, 부모는 `join()`에서 영원히 멈춘다.
실측: 워커 10개가 4분 넘게 살아 있었고 아카이브가 만들어지지 않았다.

**수정.** `Pool(initializer=_reset_signal_handlers_in_worker)`로 워커에서 SIGTERM/SIGINT를
`SIG_DFL`로 되돌리고, `try/finally`에서 `terminate()` + `join()`을 수행한다.
회귀 테스트: `test_reverse_lookup_parallel_returns_without_hanging`.

### (2) `bootout`이 앱 스텁만 정지시키고 python 자식을 고아로 남긴다

launchd의 `bootout`은 잡의 **main 프로세스(앱 스텁)** 에만 SIGTERM을 보낸다. 스텁이 죽으면
자식 python은 PID 1에 입양되어 **계속 실행**된다. 결과적으로 `stop`이 실패하고(API가 계속 응답),
`start`는 락 때문에 "이미 실행 중"이라며 복구를 거부했다.

**수정.**
- 런타임에 **부모 감시 스레드**를 추가했다(launchd 관리 모드 한정). `os.getppid() == 1`이 되면
  스스로 종료한다.
- `cmd_stop`은 bootout 뒤에도 락을 잡고 있는 프로세스를 정리한다.
- `cmd_start`은 launchd가 언로드됐는데 락만 잡고 있는 고아를 정리하고 기동한다.
- `packaging/stub.c`도 SIGTERM/SIGINT를 자식에게 전달하도록 고쳤다(다음 빌드부터 적용).
  **현재 설치된 앱은 이 수정 이전 빌드**이며, 위 런타임 감시가 같은 문제를 막는다.
  앱을 재빌드하면 cdhash가 바뀌어 부여된 권한이 무효가 되므로 지금은 재빌드하지 않았다.
- 회귀 테스트: `test_stop_cleans_up_an_orphaned_child`, `test_start_recovers_from_an_orphaned_child`.

### (3) `status`가 백엔드가 아니라 CLI 자신의 권한을 표시했다

권한은 프로세스마다 다르다. 앱이 띄운 백엔드는 손쉬운 사용을 물려받았지만, `status`를 실행한
터미널은 그렇지 않다. 그래서 백엔드가 `정상`인데 `status`는 `없음`이라고 표시했다.

**수정.** 백엔드가 실행 중이면 `/v1/status`의 판정(백엔드가 스스로 잰 값)을 우선하고,
출력에 `(권한 측정: 백엔드|터미널)`을 붙여 출처를 밝힌다.
회귀 테스트: `test_status_prefers_the_running_backend_view`, `test_status_falls_back_to_the_terminal_view`.

---

## 부록 D. M9 실기 검증 (앱 + launchd)

| 확인 | 결과 |
|---|---|
| launchd → 앱 → python 체인 | `launchd(1) → SesametokBackend.app --managed → python foreground --managed` |
| LaunchAgent `ProgramArguments` | `['/Applications/SesametokBackend.app/Contents/MacOS/SesametokBackend', '--managed']` |
| **앱의 손쉬운 사용이 자식에게 귀속** | 백엔드 `/v1/status` → `accessibility_trusted: true` |
| **같은 인터프리터를 셸에서 실행** | `accessibility_trusted: false` (격리 확인) |
| 전체 디스크 접근 | 앱에 부여하지 않았고 `false` — 그래도 `store_readable: true`(읽기 성공, ADR-017) |
| 첫 기동 | 역추적 ≈20초 → 아카이브 생성(방 411 / 메시지 9,508) → API 응답 |
| 메뉴바 | `메뉴바를 시작했습니다` 로그 확인 |
| `stop` | 1초 내 API 정지 · 락 파일 제거 · 잔여 프로세스 없음 |
| 앱 더블클릭(`open`, 인자 없음) | `start`와 동일하게 launchd로 재기동, 복구 후 `AX = true` |
| 중복 실행 | 이미 실행 중이면 아무 일도 하지 않음(`start`의 멱등성) |

---

## ADR-019. 창을 열지 않아도 발신이 동작한다 (Accepted)

**문제.** 발신이 "대상 방 창이 이미 열려 있어야 한다"에 의존하면, 사용자가 직접 그 창에서 보내는
것과 다를 바 없다. 백엔드가 스스로 방을 열고 보내야 한다.

**결정.** 두 단계로 나눈다 — **여는 단계**와 **보내는 단계**.

- **보내는 단계는 포커스 없이** 한다. ``AXValue``로 입력 상자를 채우고, 방 창의 **전송 버튼을
  ``AXPress``** 하거나 **``CGEventPostToPid``로 Enter를 카카오톡에만** 전달한다. 최전면이 아니어도
  도달한다.
- **여는 단계는 채팅 목록 행을 클릭**한다. 행은 ``AXPress``에 반응하지 않으므로 좌표 클릭이
  필요하다. 먼저 pid로 클릭해(비활성화) 시도하고, 열리지 않으면 ``NSRunningApplication``으로
  카카오톡을 활성화한 뒤 **전역 더블클릭**한다. 좌표는 **한 번 찾은 값을 재사용**한다 — 목록은
  메시지가 오면 즉시 재정렬되어, 클릭 사이에 다시 찾으면 대상을 놓친다(실측: 두 번째 검색이 0건).

**실측으로 확인한 것.**
- `CGEventPostToPid`는 존재하고 동작하지만 **행을 여는 데는 효과가 없다**(pid 더블클릭 후에도 창 수 1).
- 행을 여는 것은 **활성화 후 전역 더블클릭**이다. 따라서 이때만 카카오톡이 잠깐 앞으로 온다.
- `AXValueGetValue`는 PyObjC 바인딩이 깨져 있어(`Quartz`에 없고, `ApplicationServices` 판은 인자를
  잘못 해석) **ctypes로 직접 호출**한다. 이것이 없으면 좌표를 못 읽어 클릭 경로 전체가 죽는다.
- AX 순회는 **깊이·개수 상한**과 **메시징 타임아웃 2초**, **파이프라인 60초 데드라인**을 둔다.
  앱 전체를 반복 순회하면 발신이 수 분 걸린다.

**결과.** 방이 닫혀 있어도 `POST /v1/messages` 한 번으로 전송되고, 결과는 `sent`로 기록된다.
실측: `.......` (7자) → 17:06:39 KST에 대상 방에 도착, `send.result` 이벤트와 `send_requests`가
`sent`로 갱신됨.

**한계(정직하게).** "창이 전혀 생기지 않는" 전송은 **UI 구동으로는 불가능**하다. 대상 방 창이
없으면 만들어야 하고, 그 행 클릭은 최전면을 요구한다. 진짜 무창 전송은 카카오톡 **네트워크
프로토콜(LOCO)** 구현이 필요하며 이는 별도 규모의 작업이다. 참고 프로젝트(`~/lazykatok`)도
`ax_send.rs` 첫머리에 "대상 채팅 창이 이미 열려 있어야 한다"고 적고 있다.

## ADR-020. 폴링 주기를 메뉴바에서 바꾸고 영속화한다 (Accepted)

**결정.** 메뉴바 하위 메뉴 `동기화 주기`(2·5·10·30·60초)에서 선택한다. 선택은 즉시 폴러의 대기
주기를 바꾸고(`Event.wait` 재계산), `<data_dir>/preferences.json`에 원자적으로 저장해 재시작 후에도
유지한다. 우선순위는 저장값 > `SESAMETOK_POLL_SECONDS` > 3초이며, 잘못된 값은 무시한다.
`preferences.json`에는 주기 숫자만 들어간다(대화 데이터 없음). 저장 실패는 경고만 남기고 주기
변경은 그대로 적용한다.

---

## ADR-021. 방을 여는 책임은 사용자에게 있다 (ADR-019의 열기 단계를 대체)

**변경.** ADR-019가 도입한 "백엔드가 방을 연다"를 폐기한다. 발신은 **이미 열려 있는 창**에만
한다. 창이 없으면 열려고 시도하지 않고 ``window_not_open``(HTTP **502**)으로 즉시 실패하며,
채팅 목록과 검색 상자에는 접근하지 않는다(목록이 열려 있는지와 무관).

**이유.**
- 창을 여는 행위는 최전면을 요구한다. 열기 시도가 포커스를 빼앗고, 채팅 목록 클릭 경로는
  40초 안팎 걸려 API로 쓰기 어렵다(실측).
- 발신 대상 방을 열어 두는 것과, 가려 두거나 최소화해 두는 것은 사용자가 직접 제어할 수 있다.
  백엔드가 창을 열면 그 제어를 빼앗는다.
- 창이 열려 있으면 **전송 자체는 포커스 없이** 동작한다(ADR-019에서 확인). 열기만 사용자에게
  맡기면 "가려진 창에 포커스 이동 없이 발신"이 성립한다.

**결과.**
- 창이 열려 있으면 ``send``가 **포커스를 뺏지 않고** 동작한다(가려진 상태 그대로 발송).
  텍스트 ``send``는 활성화 폴백을 두지 않는다 — 포커스 없는 경로가 실패하면 ``unconfirmed``다.
- ``draft``는 키보드(붙여넣기)가 필요하므로 예외적으로 창을 앞으로 가져온다(사람이
  확인하는 단계이므로 의도된 동작).
- 창이 없으면 ``502`` + ``window_not_open`` + "카카오톡에서 해당 방을 연 뒤 다시 시도하세요".
- ``search_unavailable``은 더 이상 발생하지 않는다(검색 경로 제거). 열기 관련 코드
  (목록 순회·좌표 클릭·검색)도 함께 제거했고, 회귀 테스트가 그 부재를 고정한다.
- 실패 메시지에 **다른 방 이름을 넣지 않는다**(이전에는 열려 있는 창 제목을 나열했다).
