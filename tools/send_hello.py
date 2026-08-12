#!/usr/bin/env python3
"""Phase 1 acceptance check — send one message to your own phone.

    python tools/send_hello.py 919876543210
    python tools/send_hello.py 919876543210 "custom text"

If a message lands on your phone, the Meta app, access token, and phone number
ID are all wired correctly and Phase 1 is done. See docs/phase-1-meta-setup.md
for how to obtain the credentials this reads from .env.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# Windows' default console codepage (cp1252) can't encode the symbols this
# script prints — see the matching fix in run_server.py.
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from whatsapp_voice.config import Settings  # noqa: E402
from whatsapp_voice.whatsapp import WhatsAppClient, WhatsAppError  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("to", help="recipient in international format, no '+' (e.g. 919876543210)")
    parser.add_argument("message", nargs="?",
                        default="Hello from the WhatsApp Voice Agent 👋 (Phase 1 test)",
                        help="text to send")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = Settings.load()

    if not settings.whatsapp.is_configured:
        print("✗ WhatsApp is not configured.\n"
              "  Set WHATSAPP_ACCESS_TOKEN and WHATSAPP_PHONE_NUMBER_ID in .env.\n"
              "  See docs/phase-1-meta-setup.md for how to get them.")
        return 1

    try:
        client = WhatsAppClient(settings.whatsapp)
        message_id = client.send_text(args.to, args.message)
    except WhatsAppError as exc:
        print(f"✗ Send failed: {exc}")
        if exc.status_code in (401, 190):
            print("  → The access token is likely expired. Temporary tokens last ~24h; "
                  "generate a fresh one or set up a permanent token.")
        elif exc.status_code == 400:
            print("  → Check the recipient number is registered on the test app's "
                  "allowed list, and is in international format without '+'.")
        return 1

    print(f"✓ Sent to {args.to}. Message id: {message_id}")
    print("  Check your phone — if it arrived, Phase 1 is complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
