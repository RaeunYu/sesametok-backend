# AGENTS.md — 이 저장소에서 작업하기

이 문서는 이 저장소를 변경하는 AI 에이전트(그리고 사람)를 위한 것입니다. `README.md`는 제품이
무엇을 하는지, `DECISIONS.md`는 확정된 결정과 그 근거를 기록합니다. **`DECISIONS.md`가
우선합니다.**

## 반드시 지킬 것

1. **실제 대화 데이터를 영속적인 위치에 절대 남기지 않는다.** 방 이름, 닉네임, 메시지 본문,
   user id, 기기 UUID, 유도된 키, 토큰을 코드·테스트·로그 메시지·docstring·커밋·픽스처에 넣지
   않는다. 로그에는 개수, id, 상태, 문자 수만 남긴다. 테스트는 합성 데이터만 사용한다
   (`tests/support.py`).
2. **발신은 되돌릴 수 없다.** 이중 게이트(API 계층 + 발신 워커), 동의 플래그, 멱등성,
   단일 in-flight 워커, 레이트 리밋을 유지한다. `unconfirmed` 결과에 자동 재시도를 절대 추가하지
   않는다 — 중복 전송이 될 수 있다.
3. **카카오톡 DB를 절대 수정하지 않는다.** `mode=ro`로 읽기 전용으로 열며, checkpoint·vacuum·
   WAL truncate·lock 조작을 하지 않는다.
4. **AppKit은 메인 스레드 전용이다.** 백그라운드 스레드에서 `NSStatusItem`/`NSWindow`/
   `NSPasteboard`를 만지지 않는다. SQLite 연결도 스레드에 귀속되므로, 스레드마다 자신의
   `Archive`를 연다.
5. **아카이브 키를 출력하지 않는다** — 프로세스가 대화형일 때(`sys.stderr.isatty()`이고
   `--managed`가 아닐 때)를 제외하고, 로그 파일에는 절대 남기지 않는다.
6. **loopback 전용.** API는 `127.0.0.1`에만 바인딩하고, webhook 대상은 포트를 명시한 loopback
   `http`여야 한다. 이 백엔드는 외부 전달을 위해 네트워크로 나가지 않는다.

## 명령

```bash
./.venv/bin/python -m pytest -q          # 전체 스위트가 통과해야 한다
./.venv/bin/python -m pytest -q tests/test_reader_synthetic_db.py
```

테스트는 실제 카카오톡 DB, 로그인 키체인, `launchctl`에 접근해서는 안 된다.
`tests/conftest.py`의 `env` 픽스처가 데이터·로그·plist 경로를 `tmp_path`로 돌리므로 그것을
사용한다.

## 구조

| 경로 | 책임 |
|---|---|
| `config.py` | 환경변수와 모든 로컬 경로 |
| `kakao/` | 저장소 탐색, 키 유도, SQLCipher 접근, 정규화 |
| `core/` | 암호화 아카이브, 폴러, fingerprint diff, 이벤트 버스, 발신 워커 |
| `api/` | loopback HTTP/UDS, SSE, webhook 전달 |
| `app/` | 메뉴바 상태 아이콘(PyObjC) |
| `send/` | 대상 해석, Accessibility 파이프라인, 오류 코드 |
| `security/` | API 토큰, 아카이브 키 + 키체인 |
| `launcher.py`, `cli.py` | launchd 수명주기와 커맨드라인 |

## 발목을 잡는 것들

- **키 유도는 핵심 부하를 지탱한다.** `kakao/derive.py`에는 합성 known-answer 벡터(`KAT_*`)가
  있다. 리팩터가 이들을 깨뜨리면 리팩터가 잘못된 것이다 — 벡터를 코드에 맞춰 갱신하지 마라.
- **SQLCipher 키 형식이 서로 다르다.** 카카오톡 DB는 256-hex `secure_key`를 *passphrase 문자열*로
  받는다(`PRAGMA key = '<hex>'`). 우리 아카이브는 32바이트 랜덤 키를 *raw key literal*로 받는다
  (`PRAGMA key = "x'<hex>'"`). 둘을 섞으면 `file is not a database`가 난다.
- **`PRAGMA key`를 `cipher_compatibility = 3`보다 먼저.**
- **메시지 타입에는 `0x4000` 플래그가 붙는다.** text/feed/placeholder를 판정하기 전에
  `TYPE_FLAG_MASK`로 마스킹하지 않으면 실제 텍스트가 placeholder로 대체된다.
- **오픈채팅은 `NTChatRoom.linkId`로 식별한다.** `NTOpenLink`의 chat id가 아니다.
- **TCC 권한은 resolve된 실행 파일 경로에 귀속된다.** pyenv 기반 venv python은 버전이 고정된
  바이너리로 resolve되므로, Python을 바꾸면 권한이 무효가 된다.
- **launchd `KeepAlive`는 종료한 프로세스를 다시 살린다.** "종료"는 `exit`가 아니라
  `launchctl bootout`을 거쳐야 한다.
- **`AF_UNIX` 경로에는 길이 제한이 있다**(macOS에서 약 104바이트). 데이터 디렉터리가 깊으면
  `SESAMETOK_UDS_PATH`를 사용한다.

## 동작을 바꿀 때

- 테스트를 추가하거나 갱신한다. 스위트가 안전 규칙의 계약이다.
- 되돌리기 어려운 결정을 내렸다면, 그 이유를 설명하는 ADR 형식 항목을 `DECISIONS.md`에
  추가한다.
- API나 환경변수를 바꾸면 `README.md`도 함께 갱신한다.
