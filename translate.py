#!/usr/bin/env python3
"""
translate.py — Gemini 3.5 Live Translate 로 오디오 배치 번역 (전사 텍스트만 저장)

동작:
  - en2ko/  : 영어 음성  → 한국어 (targetLanguageCode=ko)
  - ko2en/  : 한국어 음성 → 영어   (targetLanguageCode=en)
  각 폴더의 .wav/.mp3 파일을 16kHz mono PCM 으로 변환하여 파일당 세션 1개로
  스트리밍하고, 세션의 "출력 전사(output transcription)" 조각들을 순서대로
  이어붙여 번역 텍스트를 만들어 outputs/<원본이름>.txt 로 저장한다.

  ※ 모델이 내는 번역 *음성*은 사용하지 않고 버린다.
  ※ 별도 STT 를 쓰지 않는다 — Live API 가 제공하는 출력 전사만 사용한다.

특징:
  - 재실행 시 결과 .txt 가 이미 있으면 스킵
  - 파일 간 딜레이(기본 10초), 429 발생 시 지수 백오프 재시도
  - 파일 단위 실패는 스킵하고 마지막에 요약 출력

사용 예:
  python translate.py                 # en2ko, ko2en 모두 처리
  python translate.py --dir en2ko     # 한 폴더만
  python translate.py --only file.wav # 특정 파일만 (해당 폴더 안에서)
  python translate.py --delay 5 --max-retries 6
"""

import argparse
import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

# ----- 환경/키 ------------------------------------------------------------
try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".flac"}
# 폴더 → 목표 언어 코드 (BCP-47)
DIR_TARGET = {"sqe-en": "ko", "sqe-ko": "en"}  # 영어→한국어, 한국어→영어
MODEL = "gemini-3.5-live-translate-preview"
INPUT_RATE = 16000          # Live Translate 입력: 16kHz mono PCM, little-endian
CHUNK_MS = 100              # 권장 청크 길이 (100ms)
CHUNK_BYTES = INPUT_RATE * 2 * CHUNK_MS // 1000   # 16-bit -> 2 bytes/sample
IDLE_TIMEOUT = 12          # 오디오 전송 종료 후, 전사가 이만큼 잠잠하면 완료로 간주
OUTPUT_DIR = Path("outputs")


# ----- ffmpeg (시스템 PATH 또는 imageio-ffmpeg 번들) ------------------------
def find_ffmpeg():
    from shutil import which

    exe = which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


FFMPEG = find_ffmpeg()


def to_pcm16k(src: Path) -> bytes:
    """오디오 파일을 16kHz mono signed-16-bit little-endian PCM(raw)으로 변환."""
    if not FFMPEG:
        raise RuntimeError(
            "ffmpeg 를 찾을 수 없습니다. `pip install imageio-ffmpeg` 또는 시스템 ffmpeg 설치 필요."
        )
    cmd = [
        FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error",
        "-i", str(src),
        "-f", "s16le", "-acodec", "pcm_s16le",
        "-ac", "1", "-ar", str(INPUT_RATE),
        "pipe:1",
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg 변환 실패: {proc.stderr.decode('utf-8','replace').strip()}")
    if not proc.stdout:
        raise RuntimeError("ffmpeg 변환 결과가 비어 있음 (손상된 오디오?)")
    return proc.stdout


# ----- 429 판별 / 지수 백오프 ---------------------------------------------
def is_rate_limit(exc: Exception) -> bool:
    s = f"{type(exc).__name__} {exc}".lower()
    return "429" in s or "resource_exhausted" in s or "rate limit" in s or "quota" in s


# ----- 한 파일 번역 (1 세션) ----------------------------------------------
async def translate_one(client, types, pcm: bytes, target_lang: str, pace: bool,
                        verbose: bool = False) -> str:
    """PCM 오디오를 스트리밍하고 출력 전사 텍스트를 이어붙여 반환."""
    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],                       # 모델은 음성을 내지만 우리는 버림
        output_audio_transcription=types.AudioTranscriptionConfig(),  # ← 출력 전사 활성화
        translation_config=types.TranslationConfig(
            target_language_code=target_lang,
            echo_target_language=True,
        ),
    )

    transcript_parts: list[str] = []

    async with client.aio.live.connect(model=MODEL, config=config) as session:
        t0 = time.monotonic()  # 세션 시작 기준 — verbose 타임스탬프용

        async def sender():
            for i in range(0, len(pcm), CHUNK_BYTES):
                chunk = pcm[i:i + CHUNK_BYTES]
                await session.send_realtime_input(
                    audio=types.Blob(data=chunk, mime_type=f"audio/pcm;rate={INPUT_RATE}")
                )
                if pace:
                    # 실시간에 가깝게 페이싱 (모델이 스트리밍 번역에 최적화돼 있음)
                    await asyncio.sleep(CHUNK_MS / 1000)
            # 오디오 종료 신호 (SDK 버전에 따라 미지원일 수 있어 best-effort)
            try:
                await session.send_realtime_input(audio_stream_end=True)
            except TypeError:
                pass
            if verbose:
                dur = len(pcm) / (INPUT_RATE * 2)
                print(f"      [+{time.monotonic()-t0:5.1f}s] ⇪ 오디오 {dur:.0f}s 전송 완료 "
                      f"(이 시점 이전에 도착한 전사는 = 실시간 스트리밍 증거)", flush=True)

        send_task = asyncio.create_task(sender())

        # receive() 제너레이터는 turn 경계에서 끝난다. 캐싱하지 말고 메시지마다 새로
        # 호출해 다음 한 건을 꺼낸다(같은 내부 큐에서 순서대로 나옴). 오디오 전송이
        # 끝난 뒤 IDLE_TIMEOUT 동안 새 전사가 없으면 완료로 간주한다.
        last = time.monotonic()
        errors = 0
        try:
            while True:
                if send_task.done() and (time.monotonic() - last) > IDLE_TIMEOUT:
                    break
                try:
                    response = await asyncio.wait_for(
                        session.receive().__anext__(), timeout=2.0
                    )
                    errors = 0
                except asyncio.TimeoutError:
                    continue
                except (StopAsyncIteration, RuntimeError, Exception) as e:
                    # 연결 종료(go_away 등). 전송이 끝났으면 정상 종료로 본다.
                    if send_task.done():
                        break
                    errors += 1
                    if errors > 10:
                        raise RuntimeError(f"수신 스트림 반복 오류: {e}")
                    await asyncio.sleep(0.2)
                    continue

                sc = getattr(response, "server_content", None)
                if not sc:
                    continue
                ot = getattr(sc, "output_transcription", None)
                if ot and getattr(ot, "text", None):
                    transcript_parts.append(ot.text)
                    last = time.monotonic()
                    if verbose:
                        print(f"      [+{time.monotonic()-t0:5.1f}s] ⇩ 전사 수신: "
                              f"{ot.text!r}", flush=True)
                # 모델 음성(model_turn / inline_data)은 의도적으로 무시한다.
        finally:
            send_task.cancel()
            try:
                await send_task
            except (asyncio.CancelledError, Exception):
                pass

    return "".join(transcript_parts).strip()


async def translate_with_retry(client, types, pcm, target, pace, max_retries, verbose=False):
    delay = 5.0
    for attempt in range(1, max_retries + 1):
        try:
            return await translate_one(client, types, pcm, target, pace, verbose)
        except Exception as e:
            if is_rate_limit(e) and attempt < max_retries:
                print(f"      · 429/쿼터 — {delay:.0f}s 후 재시도 ({attempt}/{max_retries})")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 120)
                continue
            raise


# ----- 배치 -----------------------------------------------------------------
def collect_files(only: str | None):
    jobs = []
    for folder, target in DIR_TARGET.items():
        d = Path(folder)
        if not d.is_dir():
            continue
        for f in sorted(d.iterdir()):
            if f.suffix.lower() in AUDIO_EXTS and (only is None or f.name == only):
                jobs.append((f, target))
    return jobs


async def run(args):
    if not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
        print("✗ GEMINI_API_KEY 가 없습니다. .env 를 설정하세요 (.env.example 참고).")
        sys.exit(1)

    from google import genai
    from google.genai import types

    client = genai.Client(
        api_key=os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    )

    OUTPUT_DIR.mkdir(exist_ok=True)
    jobs = collect_files(args.only)
    if args.dir:
        jobs = [(f, t) for (f, t) in jobs if f.parent.name == args.dir]

    if not jobs:
        print("처리할 오디오 파일이 없습니다. sqe-en/ 또는 sqe-ko/ 에 .wav/.mp3 를 넣으세요.")
        return

    print(f"총 {len(jobs)}개 파일 처리 (모델: {MODEL})\n")
    done, skipped, failed = [], [], []

    for idx, (src, target) in enumerate(jobs, 1):
        # 입력 폴더별 하위 디렉토리에 저장 → sqe-en/001.wav 와 sqe-ko/001.wav 처럼
        # 이름이 겹쳐도 outputs/sqe-en/001.txt vs outputs/sqe-ko/001.txt 로 분리된다.
        out_dir = OUTPUT_DIR / src.parent.name
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / (src.stem + ".txt")
        tag = f"[{idx}/{len(jobs)}] {src.parent.name}/{src.name} → {target}"
        if out_path.exists():
            print(f"{tag}  ⏭  이미 존재, 스킵 ({out_path})")
            skipped.append(src.name)
            continue

        print(f"{tag}")
        t0 = time.monotonic()
        try:
            pcm = to_pcm16k(src)
            secs = len(pcm) / (INPUT_RATE * 2)
            print(f"      · 변환 완료: {secs:.1f}s 오디오, {len(pcm)//1024}KB PCM, 스트리밍 시작…")
            text = await translate_with_retry(
                client, types, pcm, target, not args.fast, args.max_retries, args.verbose
            )
            if not text:
                raise RuntimeError("전사 텍스트가 비어 있음 (응답 없음)")
            out_path.write_text(text + "\n", encoding="utf-8")
            dt = time.monotonic() - t0
            print(f"      ✓ 완료 ({dt:.1f}s) → {out_path}  ({len(text)}자)")
            done.append(src.name)
        except Exception as e:
            dt = time.monotonic() - t0
            print(f"      ✗ 실패 ({dt:.1f}s): {type(e).__name__}: {e}")
            failed.append(src.name)

        # 파일 간 딜레이 (마지막 제외)
        if idx < len(jobs):
            time.sleep(args.delay)

    print("\n" + "=" * 56)
    print(" 요약")
    print(f"   성공 {len(done)} / 스킵 {len(skipped)} / 실패 {len(failed)}")
    if failed:
        print(f"   실패 파일: {', '.join(failed)}")
    print("=" * 56)


def parse_args():
    p = argparse.ArgumentParser(description="Gemini Live Translate 배치 번역 (전사 텍스트만 저장)")
    p.add_argument("--dir", choices=list(DIR_TARGET), help="이 폴더만 처리")
    p.add_argument("--only", help="이 파일명만 처리 (en2ko/ko2en 안에서)")
    p.add_argument("--delay", type=float, default=10.0, help="파일 간 딜레이(초), 기본 10")
    p.add_argument("--max-retries", type=int, default=5, help="429 재시도 횟수, 기본 5")
    p.add_argument("--fast", action="store_true",
                   help="실시간 페이싱 없이 최대한 빨리 전송 (디버그/테스트용)")
    p.add_argument("--verbose", action="store_true",
                   help="청크 송신/전사 수신을 타임스탬프와 함께 출력 (스트리밍 검증용)")
    return p.parse_args()


if __name__ == "__main__":
    try:
        asyncio.run(run(parse_args()))
    except KeyboardInterrupt:
        print("\n중단됨.")
