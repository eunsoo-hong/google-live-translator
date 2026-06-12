# live-translate-eval

Gemini 3.5 Live Translate API(`gemini-3.5-live-translate-preview`)로 오디오 파일을
배치 번역하고, 모델이 제공하는 **출력 전사(output transcription) 텍스트만** 저장하는
평가용 도구입니다. 번역 *음성*은 사용하지 않으며, 별도 STT도 쓰지 않습니다.

- 모델: `gemini-3.5-live-translate-preview` (2026-06-09 public preview)
- 방식: Gemini **Live API** (WebSocket), `google-genai` Python SDK
- 입력: 16kHz mono PCM 스트리밍 (ffmpeg로 자동 변환)
- 출력: Live API의 출력 전사 조각을 이어붙인 번역 텍스트 → `outputs/*.txt`

> 참고 문서
> - 모델: https://ai.google.dev/gemini-api/docs/models/gemini-3.5-live-translate-preview
> - 가이드: https://ai.google.dev/gemini-api/docs/live-api/live-translate

---

## 폴더 구조

```
.
├── check_connection.py   # 네트워크/키/WebSocket 연결 진단 (사내망 단독 실행용)
├── translate.py          # 메인 배치 번역 스크립트
├── requirements.txt
├── .env.example          # 키 이름만 명시 (복사해서 .env 작성)
├── .env                  # 실제 API 키 (gitignore, 커밋 금지)
├── sqe-en/               # 영어 음성 입력 → 한국어   (gitignore)
├── sqe-ko/               # 한국어 음성 입력 → 영어   (gitignore)
└── outputs/              # 번역 전사 결과 .txt        (gitignore)
```

`.env`, 오디오 파일(`*.wav`/`*.mp3`), `sqe-en/`, `sqe-ko/`, `outputs/` 는
`.gitignore`로 **절대 커밋되지 않습니다**(API 키·오디오 데이터·결과물 보호).

---

## 셋업

```bash
# 1) 가상환경(권장)
python3 -m venv .venv && source .venv/bin/activate

# 2) 의존성 설치
pip install -r requirements.txt

# 3) API 키 설정
cp .env.example .env
#   .env 를 열어 GEMINI_API_KEY=... 채우기 (https://aistudio.google.com/apikey)
```

프록시 환경(사내망)이라면 `.env` 또는 셸에 표준 변수를 설정하세요:

```bash
export HTTPS_PROXY=http://proxy.example.com:8080
export NO_PROXY=localhost,127.0.0.1
```

---

## 연결 확인 (먼저 실행)

사외 개발 후 **사내망 이관 시 가장 먼저** 실행하세요. 외부 의존성을 최소화해
단독 실행 가능합니다(1·2단계는 표준 라이브러리만 사용).

```bash
python check_connection.py
```

검증 항목:

1. `generativelanguage.googleapis.com` **DNS 조회 + HTTPS(443) TLS 핸드셰이크**
2. **API 키 유효성** — REST로 모델 목록 가볍게 조회
3. **Live API WebSocket 핸드셰이크** — `gemini-3.5-live-translate-preview`로
   세션을 열어 `setupComplete`만 확인하고 즉시 종료 (콘텐츠 생성 없음 → 과금 최소화)

각 단계를 `[PASS]/[FAIL]/[SKIP]`로 출력하고, 실패 시 **한국어로 원인 추정**을
보여줍니다(예: "WebSocket 차단 — 사내 프록시/방화벽 설정 확인 필요").
종료 코드: 모두 통과 `0`, 핵심(1·2) 실패 `1`, WebSocket만 실패 `2`.

---

## 실행 (배치 번역)

입력 오디오를 폴더에 넣습니다(파일당 1분 30초~3분, wav/mp3):

```
en2ko/   영어 음성 10개   → 한국어로 번역
ko2en/   한국어 음성 10개 → 영어로 번역
```

```bash
python translate.py                  # 두 폴더 모두 처리
python translate.py --dir en2ko      # 한 폴더만
python translate.py --only sample.wav  # 특정 파일만
python translate.py --delay 5 --max-retries 6
python translate.py --fast           # 실시간 페이싱 없이 빠르게 (테스트용)
```

동작:

- 각 파일을 16kHz mono PCM으로 변환 후 **파일당 세션 1개**로 스트리밍
- 출력 전사 조각을 순서대로 이어붙여 `outputs/<원본이름>.txt` 저장 (음성은 버림)
- 진행 로그(파일명·소요 시간·성공/실패) 출력, **실패 파일은 스킵** 후 요약
- **재실행 시 이미 `.txt`가 있는 파일은 스킵** (이어서 처리)
- 파일 간 딜레이(기본 10초), **429 발생 시 지수 백오프 재시도**

---

## 트러블슈팅

| 증상 | 점검 |
|---|---|
| `GEMINI_API_KEY 미설정` | `.env`에 키를 넣었는지, 셸에서 export 했는지 |
| REST는 되는데 WebSocket FAIL | 사내 프록시가 `wss` 업그레이드를 막는지 (CONNECT/Upgrade 허용) |
| TLS 핸드셰이크 FAIL | SSL 인스펙션 환경 — 사내 루트 CA를 시스템 신뢰 저장소에 등록 |
| `ffmpeg 를 찾을 수 없음` | `pip install imageio-ffmpeg`(자동 포함) 또는 시스템 ffmpeg 설치 |
| 429 반복 | `--delay` 늘리기, 쿼터 확인 |
