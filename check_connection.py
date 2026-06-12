#!/usr/bin/env python3
"""
check_connection.py — Gemini Live Translate 연결 진단 (사내망 단독 실행용)

이 프로젝트는 사외에서 개발한 뒤 사내망으로 이관하므로, 사내 환경에서 단독으로
실행해 네트워크/인증/Live API 접근을 검증하기 위한 스크립트다.

검증 단계:
  1. generativelanguage.googleapis.com DNS 조회 + HTTPS(443) TCP/TLS 연결
  2. API 키 유효성 (REST: 모델 목록 조회 — 가벼운 호출)
  3. Live API WebSocket 핸드셰이크 (gemini-3.5-live-translate-preview)
     -> setup 핸드셰이크만 확인하고 즉시 종료 (과금 최소화, 콘텐츠 생성 안 함)

각 단계를 [PASS]/[FAIL]/[SKIP]로 출력하고, 실패 시 한국어로 원인을 추정한다.

의존성:
  - 1·2단계: 표준 라이브러리만 사용 (socket, ssl, urllib, json) — 단독 실행 가능
  - 3단계: `websockets` 패키지가 있으면 검증, 없으면 [SKIP] 안내
  - .env 가 있으면 자동으로 읽고, python-dotenv 가 없어도 직접 파싱한다

프록시: HTTPS_PROXY / HTTP_PROXY / NO_PROXY 표준 환경변수를 인식한다.
"""

import json
import os
import socket
import ssl
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit

HOST = "generativelanguage.googleapis.com"
PORT = 443
MODEL = "gemini-3.5-live-translate-preview"
API_BASE = f"https://{HOST}/v1beta"
WS_URL = (
    f"wss://{HOST}/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
)
TIMEOUT = 15  # seconds

# ----- 출력 헬퍼 -----------------------------------------------------------
GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
if not sys.stdout.isatty():
    GREEN = RED = YELLOW = DIM = RESET = ""


def _pass(msg):
    print(f"  [{GREEN}PASS{RESET}] {msg}")


def _fail(msg, reason=""):
    print(f"  [{RED}FAIL{RESET}] {msg}")
    if reason:
        print(f"         ↳ {YELLOW}{reason}{RESET}")


def _skip(msg, reason=""):
    print(f"  [{YELLOW}SKIP{RESET}] {msg}")
    if reason:
        print(f"         ↳ {DIM}{reason}{RESET}")


def _hdr(title):
    print(f"\n{title}")


# ----- .env 로더 (python-dotenv 없어도 동작) --------------------------------
def load_env():
    try:
        from dotenv import load_dotenv  # type: ignore

        load_dotenv()
        return
    except Exception:
        pass
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def get_api_key():
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")


def proxy_for_https():
    """Return the HTTPS proxy URL from env (respecting NO_PROXY for our HOST)."""
    no_proxy = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    for token in (t.strip() for t in no_proxy.split(",")):
        if token and (token == HOST or (token.startswith(".") and HOST.endswith(token))):
            return None
    return (
        os.environ.get("HTTPS_PROXY")
        or os.environ.get("https_proxy")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("http_proxy")
    )


# ----- 1단계: DNS + HTTPS/TLS ----------------------------------------------
def _open_tls_socket():
    """Return a TLS-wrapped socket to HOST:443, tunnelling through a proxy if set."""
    proxy = proxy_for_https()
    ctx = ssl.create_default_context()
    if proxy:
        parts = urlsplit(proxy if "://" in proxy else "http://" + proxy)
        raw = socket.create_connection((parts.hostname, parts.port or 8080), TIMEOUT)
        connect = (
            f"CONNECT {HOST}:{PORT} HTTP/1.1\r\nHost: {HOST}:{PORT}\r\n"
            "Proxy-Connection: keep-alive\r\n\r\n"
        )
        raw.sendall(connect.encode())
        resp = raw.recv(4096).decode("latin-1", "replace")
        if " 200 " not in resp.split("\r\n", 1)[0]:
            raw.close()
            raise OSError(f"프록시 CONNECT 거부: {resp.splitlines()[0] if resp else '응답 없음'}")
        return ctx.wrap_socket(raw, server_hostname=HOST)
    raw = socket.create_connection((HOST, PORT), TIMEOUT)
    return ctx.wrap_socket(raw, server_hostname=HOST)


def step1_network():
    _hdr("1) DNS 조회 + HTTPS(443) 연결")
    ok = True

    # DNS
    try:
        infos = socket.getaddrinfo(HOST, PORT, proto=socket.IPPROTO_TCP)
        addrs = sorted({i[4][0] for i in infos})
        _pass(f"DNS 조회 성공: {HOST} → {', '.join(addrs)}")
    except socket.gaierror as e:
        _fail(
            f"DNS 조회 실패: {HOST}",
            f"이름 해석 불가 ({e}). 사내 DNS 서버가 외부 도메인을 막고 있거나 "
            "인터넷 자체가 차단된 환경일 수 있음. /etc/resolv.conf 및 사내 DNS 정책 확인.",
        )
        return False  # DNS 실패면 TLS 시도 의미 없음

    # TLS handshake
    proxy = proxy_for_https()
    try:
        sock = _open_tls_socket()
        cipher = sock.cipher()
        sock.close()
        via = f" (프록시 경유: {proxy})" if proxy else ""
        _pass(f"HTTPS/TLS 핸드셰이크 성공{via} — {cipher[0] if cipher else 'TLS'}")
    except ssl.SSLError as e:
        ok = False
        _fail(
            "TLS 핸드셰이크 실패",
            f"{e}. 사내 SSL 인스펙션(중간자 프록시)이 인증서를 교체하는 경우일 수 있음. "
            "사내 루트 CA를 시스템 신뢰 저장소에 등록했는지 확인.",
        )
    except (socket.timeout, TimeoutError):
        ok = False
        _fail(
            "HTTPS(443) 연결 타임아웃",
            "방화벽이 443 아웃바운드를 막거나, 프록시를 거쳐야 하는데 HTTPS_PROXY 가 "
            "설정되지 않았을 수 있음.",
        )
    except OSError as e:
        ok = False
        _fail("HTTPS(443) 연결 실패", f"{e}. 방화벽/프록시 설정 확인 필요.")
    return ok


# ----- 2단계: API 키 유효성 (REST) -----------------------------------------
def step2_api_key():
    _hdr("2) API 키 유효성 (REST 모델 목록 조회)")
    key = get_api_key()
    if not key:
        _fail(
            "GEMINI_API_KEY 미설정",
            ".env 에 GEMINI_API_KEY 를 넣거나 환경변수로 export 하세요. "
            "(.env.example 참고)",
        )
        return False

    # urllib는 HTTPS_PROXY/HTTP_PROXY 환경변수를 자동 인식한다.
    url = f"{API_BASE}/models?key={key}&pageSize=1"
    req = urllib.request.Request(url, headers={"User-Agent": "check_connection/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        n = len(data.get("models", []))
        _pass(f"API 키 유효 — 모델 목록 조회 성공 (응답 수신, 모델 {n}건+)")
        return True
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        if e.code in (400, 401, 403):
            _fail(
                f"API 키 거부됨 (HTTP {e.code})",
                "키가 잘못됐거나 비활성/권한 부족. AI Studio에서 키를 재발급하거나 "
                f"Generative Language API 사용 설정을 확인. 서버 메시지: {_short(body)}",
            )
        elif e.code == 429:
            _fail(f"레이트 리밋 (HTTP 429)", "쿼터 초과. 잠시 후 재시도.")
        else:
            _fail(f"REST 호출 실패 (HTTP {e.code})", _short(body))
        return False
    except urllib.error.URLError as e:
        _fail(
            "REST 엔드포인트 도달 실패",
            f"{e.reason}. 프록시 뒤라면 HTTPS_PROXY 설정 확인, 방화벽이 "
            "generativelanguage.googleapis.com 을 막는지 확인.",
        )
        return False


# ----- 3단계: Live API WebSocket 핸드셰이크 --------------------------------
def step3_websocket():
    _hdr(f"3) Live API WebSocket 핸드셰이크 ({MODEL})")
    key = get_api_key()
    if not key:
        _skip("API 키가 없어 WebSocket 검증 생략", "2단계 먼저 통과 필요.")
        return None

    try:
        import asyncio

        import websockets
    except ImportError:
        _skip(
            "`websockets` 패키지 없음 → WebSocket 검증 생략",
            "pip install websockets 후 다시 실행하면 Live API 핸드셰이크까지 검증함.",
        )
        return None

    async def handshake():
        url = f"{WS_URL}?key={key}"
        # websockets는 버전에 따라 proxy 인자 지원이 다르므로 best-effort로 처리.
        kwargs = {"open_timeout": TIMEOUT, "close_timeout": 5}
        proxy = proxy_for_https()
        try:
            conn = websockets.connect(url, **({"proxy": proxy} if proxy else {}), **kwargs)
        except TypeError:
            # 구버전 websockets: proxy 인자 미지원
            conn = websockets.connect(url, **kwargs)
        async with conn as ws:
            # setup 메시지만 보내 핸드셰이크 확인 (콘텐츠/오디오 전송 없음 → 과금 최소화)
            await ws.send(json.dumps({"setup": {"model": f"models/{MODEL}"}}))
            raw = await asyncio.wait_for(ws.recv(), timeout=TIMEOUT)
            msg = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
            return msg

    try:
        msg = asyncio.run(handshake())
    except Exception as e:  # websockets 예외 계층이 버전마다 달라 광범위 캐치
        name = type(e).__name__
        if "InvalidStatus" in name or "403" in str(e) or "401" in str(e):
            _fail(
                "WebSocket 인증/권한 거부",
                f"{e}. API 키가 Live API 권한이 없거나 모델 미허용일 수 있음.",
            )
        elif isinstance(e, (TimeoutError, socket.timeout)):
            _fail(
                "WebSocket 연결/응답 타임아웃",
                "사내 프록시·방화벽이 WebSocket(wss) 업그레이드를 차단할 가능성 높음. "
                "프록시가 HTTP CONNECT 터널 및 Upgrade 헤더를 허용하는지 확인 필요.",
            )
        else:
            _fail(
                "WebSocket 연결 실패",
                f"{name}: {e}. 다수 프록시가 일반 HTTPS는 통과시켜도 WebSocket 업그레이드는 "
                "막는다. 사내 프록시/방화벽의 wss 허용 정책 확인 필요.",
            )
        return False

    if "setupComplete" in msg or "setup_complete" in msg.lower():
        _pass("WebSocket 핸드셰이크 성공 — setupComplete 수신, 세션 종료 (과금 없음)")
        return True
    _fail("WebSocket 응답 비정상", f"setupComplete 미수신. 응답: {_short(msg)}")
    return False


def _short(text, n=200):
    text = " ".join((text or "").split())
    return text[:n] + ("…" if len(text) > n else "")


# ----- main ----------------------------------------------------------------
def main():
    load_env()
    print("=" * 64)
    print(" Gemini Live Translate 연결 진단")
    print(f" 대상: {HOST}  |  모델: {MODEL}")
    proxy = proxy_for_https()
    print(f" 프록시: {proxy if proxy else '미설정 (직접 연결)'}")
    print("=" * 64)

    r1 = step1_network()
    r2 = step2_api_key() if r1 else (_skip_chain("2", "1단계 실패") or False)
    r3 = step3_websocket() if r2 else (_skip_chain("3", "2단계 실패") or None)

    print("\n" + "=" * 64)
    print(" 요약")
    print(f"   1) 네트워크/TLS : {_mark(r1)}")
    print(f"   2) API 키       : {_mark(r2)}")
    print(f"   3) WebSocket    : {_mark(r3)}")
    print("=" * 64)

    # 종료 코드: 필수 단계(1,2) 실패 시 1. WebSocket SKIP은 성공으로 간주하지 않되 비치명.
    if not (r1 and r2):
        print(f"{YELLOW}→ 사내망 이관 전, 위 [FAIL] 항목을 네트워크/보안팀과 함께 해결하세요.{RESET}")
        sys.exit(1)
    if r3 is False:
        print(f"{YELLOW}→ REST는 되지만 WebSocket이 막혀 있습니다. Live API에는 wss 허용이 필수입니다.{RESET}")
        sys.exit(2)
    print(f"{GREEN}→ 모든 핵심 점검 통과. translate.py 실행 가능.{RESET}")
    sys.exit(0)


def _skip_chain(step, reason):
    _hdr(f"{step}) 생략")
    _skip("선행 단계 실패로 생략", reason)


def _mark(v):
    if v is True:
        return f"{GREEN}PASS{RESET}"
    if v is False:
        return f"{RED}FAIL{RESET}"
    return f"{YELLOW}SKIP{RESET}"


if __name__ == "__main__":
    main()
