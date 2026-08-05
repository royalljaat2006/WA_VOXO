#!/usr/bin/env python3
"""Put the local server on a public HTTPS URL, and check Meta will accept it.

Meta only talks to a public HTTPS endpoint. Everything else is built and tested;
this is the one piece of plumbing between "it works on this laptop" and "it works
in WhatsApp".

    .venv/bin/python tools/run_server.py       # terminal 1
    .venv/bin/python tools/start_tunnel.py     # terminal 2

It starts a tunnel, waits for the URL, then **verifies the webhook handshake
through the public URL** before printing anything to paste into Meta. That check
is the point: it fails here, in a terminal with a readable error, rather than as
Meta's "The callback URL or verify token couldn't be validated", which says
nothing about which of the two was wrong.

Run by a human, never imported — like everything else in `tools/`.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import requests  # noqa: E402

from whatsapp_voice.config import Settings  # noqa: E402

#: cloudflared prints its URL on stderr, mixed into ordinary log lines.
_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
_NGROK_URL = re.compile(r"https://[a-z0-9-]+\.ngrok[-.][a-z.]+")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--tool", choices=("cloudflared", "ngrok"), default=None,
        help="Default: cloudflared, which needs no account for a quick tunnel.",
    )
    args = parser.parse_args()

    settings = Settings.load()
    tool = args.tool or _pick_tool()
    if tool is None:
        print("✗ Neither cloudflared nor ngrok is installed.")
        print("  brew install cloudflared")
        return 1

    if not _server_is_up(args.port):
        print(f"✗ Nothing is listening on localhost:{args.port}.")
        print("  Start it first:  .venv/bin/python tools/run_server.py")
        return 1

    verify_token = settings.whatsapp.verify_token
    if not verify_token:
        print("✗ WHATSAPP_VERIFY_TOKEN is not set in .env.")
        print("  You invent this value — any random string — and paste the same")
        print("  one into Meta. Without it the handshake cannot succeed.")
        return 1

    print(f"▶  Starting {tool} → localhost:{args.port} …")
    process = _start(tool, args.port)

    try:
        url = _wait_for_url(process, tool)
        if not url:
            print("✗ The tunnel did not produce a URL. Its output is above.")
            return 1

        print(f"✓  Public URL: {url}")

        # A failed check is a warning, not a stop. It can fail for reasons that
        # have nothing to do with the setup — a sandboxed shell that cannot
        # resolve a brand-new hostname, for one — and killing the tunnel over
        # that would throw away a working URL.
        if not _handshake_works(url, verify_token):
            print("\n⚠  The check did not pass from this machine. That is not proof")
            print("   the setup is wrong — try the URL in a browser, and if /health")
            print("   answers, go ahead and click Verify in Meta.")

        _print_meta_instructions(url, settings)
        print("\n   Leave this running. Ctrl-C closes the tunnel.\n")
        process.wait()
    except KeyboardInterrupt:
        print("\n▶  Tunnel closed.")
    finally:
        process.terminate()

    return 0


def _pick_tool() -> str | None:
    for candidate in ("cloudflared", "ngrok"):
        if shutil.which(candidate):
            return candidate
    return None


def _server_is_up(port: int) -> bool:
    try:
        requests.get(f"http://localhost:{port}/health", timeout=3)
    except requests.RequestException:
        return False
    return True


def _start(tool: str, port: int) -> subprocess.Popen:
    command = (
        ["cloudflared", "tunnel", "--url", f"http://localhost:{port}"]
        if tool == "cloudflared"
        else ["ngrok", "http", str(port), "--log", "stdout"]
    )
    return subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )


def _wait_for_url(process: subprocess.Popen, tool: str, timeout_s: int = 40) -> str | None:
    pattern = _URL if tool == "cloudflared" else _NGROK_URL
    deadline = time.monotonic() + timeout_s

    while time.monotonic() < deadline:
        line = process.stdout.readline() if process.stdout else ""
        if not line:
            if process.poll() is not None:
                return None
            continue
        match = pattern.search(line)
        if match:
            return match.group(0)
    return None


def _handshake_works(url: str, verify_token: str) -> bool:
    """Do Meta's verification GET ourselves, through the public URL.

    Meta's own failure message names neither the URL nor the token, so finding
    out here — where the status code and body are visible — is worth the extra
    two seconds.
    """
    print("▶  Checking the webhook handshake through the public URL …")
    challenge = "handshake-check"
    response = None
    last_error: Exception | None = None

    # A fresh quick-tunnel hostname takes a few seconds to resolve. Meta hits the
    # same wall if you click Verify too early, so waiting here is the same wait,
    # just with a useful message attached.
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        try:
            response = requests.get(
                f"{url}/webhook",
                params={
                    "hub.mode": "subscribe",
                    "hub.verify_token": verify_token,
                    "hub.challenge": challenge,
                },
                timeout=15,
            )
            break
        except requests.RequestException as exc:
            last_error = exc
            print("   … DNS not ready yet, retrying")
            time.sleep(3)

    if response is None:
        print(f"✗ Could not reach the tunnel after 45s: {type(last_error).__name__}")
        print("  The tunnel is up but its hostname is not resolving from here.")
        print(f"  Try it in a browser: {url}/health")
        return False



    if response.status_code == 403:
        print("✗ The server rejected the verify token.")
        print("  The value in .env is what Meta must be given. They must match exactly.")
        return False

    if response.text.strip() != challenge:
        print(f"✗ Expected the challenge echoed back, got HTTP {response.status_code}: "
              f"{response.text[:120]!r}")
        return False

    print("✓  Handshake works — Meta will accept this URL and token.")
    return True


def _print_meta_instructions(url: str, settings: Settings) -> None:
    print("\n" + "─" * 68)
    print("PASTE INTO META  (App dashboard → WhatsApp → Configuration → Webhook)")
    print("─" * 68)
    print(f"  Callback URL:  {url}/webhook")
    print(f"  Verify token:  {settings.whatsapp.verify_token}")
    print("\n  Then click Verify and save, and under Webhook fields subscribe to")
    print("  'messages'. Nothing arrives without that subscription.")
    print("─" * 68)

    warnings = []
    if not settings.whatsapp.access_token:
        warnings.append("WHATSAPP_ACCESS_TOKEN is empty — replies cannot be sent.")
    if not settings.whatsapp.app_secret:
        warnings.append(
            "WHATSAPP_APP_SECRET is empty — incoming webhooks are NOT "
            "signature-verified. Fine for a first test on a URL nobody knows; "
            "set it before this is public for any length of time."
        )
    if not settings.auth.enabled:
        warnings.append(
            "AUTH_ENABLED=false — every number that reaches this server is "
            "answered, and this URL is now public."
        )
    elif not settings.auth.allowed_numbers:
        warnings.append(
            "The allowlist is empty, so every request will be denied. Set "
            "WHATSAPP_ALLOWED_NUMBERS=<your number>:admin+pay"
        )
    if settings.payments.enabled and settings.payments.moves_real_money:
        warnings.append("PAYMENT_GATEWAY moves REAL MONEY on a public URL.")

    if warnings:
        print("\n⚠  Before you send a message:")
        for warning in warnings:
            print(f"   · {warning}")


if __name__ == "__main__":
    raise SystemExit(main())
