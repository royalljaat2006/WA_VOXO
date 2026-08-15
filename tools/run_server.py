#!/usr/bin/env python3
"""Run the webhook server locally.

    python tools/run_server.py
    python tools/run_server.py --port 9000 --reload

Then expose it publicly so Meta can reach it:

    ngrok http 8000

and point the Meta webhook config at `https://<ngrok-id>.ngrok-free.app/webhook`.
Full steps: docs/phase-2-webhook.md
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# Windows' default console codepage (cp1252) can't encode the symbols below
# (▶ ✗ ⚠ 🔒 …), which crashes every print before the server ever binds a port —
# indistinguishable from the server just not starting. UTF-8 stdout fixes it.
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from whatsapp_voice import logging_setup  # noqa: E402
from whatsapp_voice.config import Settings  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true", help="restart on code changes")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = Settings.load()
    logging_setup.configure(level=settings.log_level, fmt=settings.log_format)
    if not settings.whatsapp.is_configured:
        print("✗ WhatsApp is not configured — the server can receive messages but "
              "cannot reply.\n"
              "  Set WHATSAPP_ACCESS_TOKEN and WHATSAPP_PHONE_NUMBER_ID in .env "
              "(docs/phase-1-meta-setup.md).")
        return 1
    if not settings.whatsapp.verify_token:
        print("✗ WHATSAPP_VERIFY_TOKEN is not set — Meta's verification handshake "
              "will fail.\n  Invent any random string, put it in .env, and paste the "
              "same value into the Meta webhook config.")
        return 1
    if not settings.whatsapp.app_secret:
        print("⚠  WHATSAPP_APP_SECRET is not set — incoming webhooks will NOT be "
              "signature-verified.\n   Fine for a first local test; set it before "
              "deploying anywhere public.")

    engine = settings.stt.whisper_model if settings.stt.run_local_whisper else "API engine"

    if settings.handler == "transcribe":
        print(f"▶  Handler: transcribe (voice notes → text), engine: {engine}")
        print("   The model loads at startup; the first run may download it.")
    elif settings.handler == "agent":
        if not settings.agent.is_configured:
            key_name = ("NVIDIA_API_KEY" if settings.agent.provider == "nemotron"
                        else "ANTHROPIC_API_KEY")
            print(f"✗ HANDLER=agent but {key_name} is not set "
                  f"(provider={settings.agent.provider}, docs/phase-4-agent.md).")
            return 1
        print(f"▶  Handler: agent (voice notes → answers), speech engine: {engine}")
        if settings.agent.provider == "nemotron":
            print(f"   Provider: nemotron, model: {settings.agent.nemotron_model}, "
                  f"temperature: {settings.agent.temperature}")
        else:
            print(f"   Provider: claude, model: {settings.agent.model}, "
                  f"effort: {settings.agent.effort}")
        if settings.agent.data_source == "stub":
            print("⚠  Campaign data source is STUB — every number in every reply is "
                  "FAKE.\n   Do not show these figures to anyone as real data.")
    else:
        print(f"▶  Handler: {settings.handler}. Set HANDLER=transcribe or "
              "HANDLER=agent in .env for more.")

    print("⚠  NO ACCESS CONTROL — every number that reaches this server will be "
          "answered, payments included.")

    import uvicorn

    uvicorn.run(
        "whatsapp_voice.whatsapp.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
