#!/usr/bin/env python3
"""Phase 0 — run every configured STT engine over the same audio and compare.

    python tools/benchmark_stt.py
    python tools/benchmark_stt.py --audio-dir data/audio --ground-truth truth.json

The decision this feeds: does any engine hear Hinglish plus our campaign jargon
well enough to build on? Read the transcripts; the numbers are secondary.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Allow running straight from a checkout without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from whatsapp_voice import audio as audio_utils  # noqa: E402
from whatsapp_voice.config import PROJECT_ROOT, Settings  # noqa: E402
from whatsapp_voice.metrics import word_error_rate  # noqa: E402
from whatsapp_voice.stt import TranscriptionError, build_transcribers  # noqa: E402


@dataclass
class EngineResult:
    engine: str
    text: str | None = None
    latency_s: float = 0.0
    language: str | None = None
    wer: float | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class FileResult:
    filename: str
    duration_s: float
    ground_truth: str | None = None
    engines: list[EngineResult] = field(default_factory=list)


# ----------------------------------------------------------------- benchmark

def run_benchmark(audio_dir: Path, cache_dir: Path, ground_truth: dict[str, str],
                  settings: Settings) -> list[FileResult]:
    audio_utils.require_ffmpeg()

    files = audio_utils.discover(audio_dir)
    if not files:
        raise SystemExit(
            f"No audio files in {audio_dir}.\n"
            "Drop your voice notes there first — see docs/phase-0-stt-benchmark.md"
        )

    engines = build_transcribers(settings.stt)
    if not engines:
        raise SystemExit(
            "No STT engine configured. Set an API key in .env, "
            "or leave RUN_LOCAL_WHISPER=1."
        )

    print(f"{len(files)} file(s), {len(engines)} engine(s): "
          f"{', '.join(e.name for e in engines)}")

    # Load models before timing anything, otherwise the first file absorbs the
    # model-load cost and its latency is meaningless. An engine that cannot warm
    # up is dropped rather than failing the whole run.
    ready: list = []
    for engine in engines:
        try:
            engine.warmup()
        except Exception as exc:  # noqa: BLE001 - engines raise varied types
            print(f"   ! {engine.name} unavailable, skipping: {exc}")
            continue
        ready.append(engine)

    if not ready:
        raise SystemExit("Every engine failed to initialise — see errors above.")
    engines = ready
    print()

    results: list[FileResult] = []
    for path in files:
        wav = audio_utils.to_wav(path, cache_dir)
        truth = ground_truth.get(path.name)
        file_result = FileResult(
            filename=path.name,
            duration_s=audio_utils.duration_seconds(wav),
            ground_truth=truth,
        )
        print(f"→ {path.name} ({file_result.duration_s:.1f}s)")

        for engine in engines:
            try:
                transcript = engine.transcribe(wav)
            except TranscriptionError as exc:
                file_result.engines.append(EngineResult(engine=engine.name, error=str(exc)))
                print(f"   {engine.name:<28} FAILED   {exc}")
                continue

            result = EngineResult(
                engine=engine.name,
                text=transcript.text,
                latency_s=transcript.latency_s,
                language=transcript.language,
                wer=word_error_rate(truth, transcript.text) if truth else None,
            )
            file_result.engines.append(result)
            print(f"   {engine.name:<28} {result.latency_s:>5.1f}s  {transcript.text[:70]}")

        results.append(file_result)
        print()

    return results


# ----------------------------------------------------------------- reporting

def render_markdown(results: list[FileResult]) -> str:
    engine_names: list[str] = []
    for file_result in results:
        for engine_result in file_result.engines:
            if engine_result.engine not in engine_names:
                engine_names.append(engine_result.engine)

    lines = ["# STT Comparison — Phase 0", ""]

    for file_result in results:
        lines.append(f"## {file_result.filename} · {file_result.duration_s:.1f}s")
        lines.append("")
        if file_result.ground_truth:
            lines += [f"**Said:** {file_result.ground_truth}", ""]

        for engine_result in file_result.engines:
            if not engine_result.ok:
                lines.append(f"- **{engine_result.engine}** — ⚠️ `{engine_result.error}`")
                continue
            meta = f"{engine_result.latency_s:.1f}s"
            if engine_result.wer is not None:
                meta += f" · WER {engine_result.wer:.0%}"
            if engine_result.language:
                meta += f" · {engine_result.language}"
            lines.append(f"- **{engine_result.engine}** _({meta})_")
            lines.append(f"  > {engine_result.text or '_(empty)_'}")
        lines.append("")

    lines += ["---", "", "## Summary", "",
              "| Engine | Avg latency | Avg WER | Failures |",
              "|---|---|---|---|"]

    for name in engine_names:
        entries = [e for fr in results for e in fr.engines if e.engine == name]
        ok = [e for e in entries if e.ok]
        latency = sum(e.latency_s for e in ok) / len(ok) if ok else 0.0
        wers = [e.wer for e in ok if e.wer is not None]
        wer_text = f"{sum(wers) / len(wers):.0%}" if wers else "—"
        lines.append(
            f"| {name} | {latency:.1f}s | {wer_text} | {len(entries) - len(ok)} |"
        )

    lines += [
        "",
        "_WER appears only when a ground-truth file is supplied. For Hinglish it "
        "punishes script choice (Devanagari vs Latin) as harshly as real errors — "
        "read the transcripts before trusting the number._",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------- main

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--audio-dir", type=Path, default=PROJECT_ROOT / "data" / "audio",
                        help="directory of voice notes (default: data/audio)")
    parser.add_argument("--ground-truth", type=Path,
                        default=PROJECT_ROOT / "data" / "ground_truth.json",
                        help="optional JSON of {filename: what was said}, enables WER")
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "data" / "results",
                        help="where to write comparison.md and raw.json")
    parser.add_argument("--cache-dir", type=Path, default=PROJECT_ROOT / "data" / ".cache",
                        help="converted WAV cache")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = Settings.load()

    ground_truth: dict[str, str] = {}
    if args.ground_truth.exists():
        ground_truth = json.loads(args.ground_truth.read_text(encoding="utf-8"))
        print(f"Ground truth loaded for {len(ground_truth)} file(s).")

    results = run_benchmark(args.audio_dir, args.cache_dir, ground_truth, settings)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "raw.json").write_text(
        json.dumps([asdict(r) for r in results], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    report_path = args.output_dir / "comparison.md"
    report_path.write_text(render_markdown(results), encoding="utf-8")

    print(f"✅ Report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
