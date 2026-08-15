"""Typed settings, loaded once from the environment.

Every phase adds its own block here rather than reading os.environ ad hoc, so
there is a single place to see what the system needs to run.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _csv(name: str, default: str) -> tuple[str, ...]:
    raw = os.getenv(name, default)
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _optional(name: str) -> str | None:
    value = (os.getenv(name) or "").strip()
    return value or None


@dataclass(frozen=True)
class SttSettings:
    """Speech-to-text engine configuration (Phase 0, reused from Phase 3)."""

    run_local_whisper: bool = True
    whisper_model: str = "large-v3"
    # None means auto-detect. Forcing "hi" vs "en" materially changes Hinglish
    # output (Devanagari vs Latin script), so it is worth benchmarking both.
    whisper_language: str | None = None

    openai_api_key: str | None = None
    openai_models: tuple[str, ...] = ("whisper-1", "gpt-4o-transcribe")

    sarvam_api_key: str | None = None
    sarvam_model: str = "saarika:v2.5"
    #: "unknown" asks saarika:v2.5 to detect the language itself and return the
    #: tag it settled on. That default is what makes the bot answer a Tamil
    #: speaker in Tamil: the detected tag is threaded into the system prompt.
    #: Pinning a specific tag ("hi-IN") is faster and slightly more accurate for
    #: a known-monolingual deployment, and is still available — but it silently
    #: mistranscribes every other language, which is worse for this product.
    #: Older saarika versions require an explicit tag.
    sarvam_language: str = "unknown"

    elevenlabs_api_key: str | None = None
    elevenlabs_model: str = "scribe_v1"

    #: Path to a Google Cloud service-account JSON key file. None (the
    #: default) skips the engine entirely — see stt/google_stt.py.
    google_credentials_path: str | None = None
    google_model: str = "latest_long"
    #: Primary BCP-47 language. Google's `recognize` has no "auto-detect" like
    #: Sarvam's "unknown" — the closest equivalent is naming the two languages
    #: actually spoken and letting `alternative_language_codes` catch whichever
    #: one isn't primary.
    google_language: str = "hi-IN"
    google_alternative_languages: tuple[str, ...] = ("en-IN",)

    request_timeout_s: int = 180

    #: Which engine `build_primary_transcriber` picks for production (Phase 3+).
    #: A name prefix matched against each Transcriber's `.name` (e.g. "sarvam"
    #: matches "sarvam/saarika:v2.5"). None falls back to the first configured
    #: engine, so a checkout with no STT_ENGINE set still runs on local Whisper
    #: alone rather than erroring.
    #:
    #: The project's own choice — set via STT_ENGINE=sarvam in .env, not
    #: hardcoded here — came from a real benchmark on data/audio: Sarvam ran
    #: ~15x faster than local Whisper (0.7s vs ~11s avg) and was more accurate
    #: on Hindi/Hinglish. See docs/phase-0-stt-benchmark.md § Results.
    engine: str | None = None

    #: A second name prefix, tried only if `engine` raises. Deliberately
    #: independent of the benchmark result above — this is a resilience choice
    #: (survive the primary engine's key/network failing), not a quality one.
    #: Whichever of the two actually produced a transcript is what `Transcript
    #: .engine` reports, not this wrapper.
    fallback_engine: str | None = None

    @classmethod
    def from_env(cls) -> SttSettings:
        return cls(
            run_local_whisper=_flag("RUN_LOCAL_WHISPER", True),
            whisper_model=os.getenv("WHISPER_MODEL", "large-v3"),
            whisper_language=_optional("WHISPER_LANG"),
            openai_api_key=_optional("OPENAI_API_KEY"),
            openai_models=_csv("OPENAI_MODELS", "whisper-1,gpt-4o-transcribe"),
            sarvam_api_key=_optional("SARVAM_API_KEY"),
            sarvam_model=os.getenv("SARVAM_MODEL", "saarika:v2.5"),
            sarvam_language=os.getenv("SARVAM_LANG", "unknown"),
            elevenlabs_api_key=_optional("ELEVENLABS_API_KEY"),
            elevenlabs_model=os.getenv("ELEVENLABS_MODEL", "scribe_v1"),
            google_credentials_path=_optional("GOOGLE_APPLICATION_CREDENTIALS"),
            google_model=os.getenv("GOOGLE_STT_MODEL", "latest_long"),
            google_language=os.getenv("GOOGLE_STT_LANGUAGE", "hi-IN"),
            google_alternative_languages=_csv("GOOGLE_STT_ALT_LANGUAGES", "en-IN"),
            request_timeout_s=int(os.getenv("STT_TIMEOUT_S", "180")),
            engine=_optional("STT_ENGINE"),
            fallback_engine=_optional("STT_ENGINE_FALLBACK"),
        )


@dataclass(frozen=True)
class WhatsAppSettings:
    """WhatsApp Business Cloud API configuration (Phase 1 onward).

    Credentials come from the Meta app dashboard — see docs/phase-1-meta-setup.md.
    None means "not configured yet"; the client raises a clear error rather than
    sending a broken request.
    """

    access_token: str | None = None
    phone_number_id: str | None = None
    # Shared secret we hand Meta so it can prove a webhook GET is really from us
    # (Phase 2). We choose this value; it is not issued by Meta.
    verify_token: str | None = None
    # App secret, used to verify the X-Hub-Signature-256 on incoming webhooks
    # (Phase 2). Optional until then.
    app_secret: str | None = None
    api_version: str = "v21.0"
    graph_base_url: str = "https://graph.facebook.com"
    request_timeout_s: int = 30

    @classmethod
    def from_env(cls) -> WhatsAppSettings:
        return cls(
            access_token=_optional("WHATSAPP_ACCESS_TOKEN"),
            phone_number_id=_optional("WHATSAPP_PHONE_NUMBER_ID"),
            verify_token=_optional("WHATSAPP_VERIFY_TOKEN"),
            app_secret=_optional("WHATSAPP_APP_SECRET"),
            api_version=os.getenv("WHATSAPP_API_VERSION", "v21.0"),
            graph_base_url=os.getenv("WHATSAPP_GRAPH_BASE_URL", "https://graph.facebook.com"),
            request_timeout_s=int(os.getenv("WHATSAPP_TIMEOUT_S", "30")),
        )

    @property
    def is_configured(self) -> bool:
        """True once we can actually send a message."""
        return bool(self.access_token and self.phone_number_id)


@dataclass(frozen=True)
class AgentSettings:
    """LLM agent configuration (Phase 4).

    Two providers are supported, selected by `AGENT_PROVIDER`:

    - **claude** (default) — Anthropic's Messages API.
    - **nemotron** — NVIDIA NIM's OpenAI-compatible API.

    They need separate settings blocks rather than one shared one because the
    request surfaces genuinely differ: Claude takes `effort` and rejects
    `temperature`, NVIDIA is the other way round. Sharing one field for both
    would mean sending a parameter that 400s on whichever provider is not
    selected.

    Model choice reasoning is in docs/phase-4-agent.md.
    """

    provider: str = "claude"

    # --- Anthropic ---
    api_key: str | None = None
    model: str = "claude-sonnet-5"
    #: low | medium | high | xhigh | max. `medium` because tool-selection
    #: correctness matters more than shaving a second, but the task is not hard
    #: enough to need `high`. Claude only.
    effort: str = "medium"

    # --- NVIDIA NIM / Nemotron ---
    nvidia_api_key: str | None = None
    #: Not `nvidia/nemotron-3-ultra-550b-a55b`, which this was until it was
    #: measured: on NVIDIA's hosted endpoint that model never answers at all —
    #: it is listed in `/v1/models` but every `/chat/completions` call hangs
    #: until the client gives up. The symptom is silence, not an error: the
    #: SDK retries three times at 60s each, so a WhatsApp user waits three
    #: minutes and then gets "Agent failed". This one answers in ~16s with a
    #: correctly-formed tool call, which is what the payment flow needs.
    #: (`nemotron-3-nano-30b-a3b` answers in 3s but emits malformed tool
    #: arguments — fast and wrong is the worst option for money.)
    nemotron_model: str = "nvidia/llama-3.3-nemotron-super-49b-v1.5"
    nvidia_base_url: str = "https://integrate.api.nvidia.com/v1"

    # --- Google Gemini ---
    #: ⚠️ On the *unpaid* tier Google uses prompts and responses to improve its
    #: products, and human reviewers may read them — which here means payee
    #: numbers, UPI IDs and amounts. The PIN is never exposed (the gate runs in
    #: front of the agent), but the rest is. See agent/gemini_agent.py.
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-3.6-flash"
    #: Google's OpenAI-dialect endpoint, so no second SDK is needed. The
    #: trailing slash matters — the SDK joins paths onto it.
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"

    #: Low, because picking the wrong tool or date is the failure that matters
    #: here — variety is worth nothing. Rejected by Claude, accepted by the
    #: OpenAI-compatible providers.
    temperature: float = 0.2

    # --- shared ---
    #: 1024 was sized for the answer alone, which was right when no model
    #: thought before replying. On the current Claude models adaptive thinking
    #: is ON when `thinking` is unset, and `max_tokens` caps thinking **plus**
    #: response text — so a reply that needs 80 tokens can still be truncated
    #: mid-sentence by the thinking that preceded it. This is a ceiling, not a
    #: spend: unused headroom costs nothing, a truncated payment confirmation
    #: costs a demo.
    max_tokens: int = 8192
    #: Ceiling on model turns per user message. A tool that keeps failing would
    #: otherwise loop until the bill notices.
    max_tool_turns: int = 6
    max_history_turns: int = 20
    conversation_idle_timeout_s: float = 3600.0
    request_timeout_s: int = 60
    #: Which CampaignDataSource backs the tools. Only "stub" exists until eko
    #: API access lands — see agent/datasource.py.
    data_source: str = "stub"

    @classmethod
    def from_env(cls) -> AgentSettings:
        return cls(
            provider=os.getenv("AGENT_PROVIDER", "claude").strip().lower(),
            api_key=_optional("ANTHROPIC_API_KEY"),
            model=os.getenv("AGENT_MODEL", "claude-sonnet-5"),
            effort=os.getenv("AGENT_EFFORT", "medium").strip().lower(),
            nvidia_api_key=_optional("NVIDIA_API_KEY"),
            nemotron_model=os.getenv(
                "NEMOTRON_MODEL", "nvidia/llama-3.3-nemotron-super-49b-v1.5"
            ),
            nvidia_base_url=os.getenv(
                "NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1"
            ),
            gemini_api_key=_optional("GEMINI_API_KEY"),
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.6-flash"),
            gemini_base_url=os.getenv(
                "GEMINI_BASE_URL",
                "https://generativelanguage.googleapis.com/v1beta/openai/",
            ),
            temperature=float(os.getenv("AGENT_TEMPERATURE", "0.2")),
            max_tokens=int(os.getenv("AGENT_MAX_TOKENS", "1024")),
            max_tool_turns=int(os.getenv("AGENT_MAX_TOOL_TURNS", "6")),
            max_history_turns=int(os.getenv("AGENT_MAX_HISTORY_TURNS", "20")),
            conversation_idle_timeout_s=float(
                os.getenv("AGENT_IDLE_TIMEOUT_S", "3600")
            ),
            request_timeout_s=int(os.getenv("AGENT_TIMEOUT_S", "60")),
            data_source=os.getenv("AGENT_DATA_SOURCE", "stub").strip().lower(),
        )

    @property
    def active_model(self) -> str:
        """The model the selected provider will actually use.

        Anything reporting "which model is running" — logs, /health, the demo
        UI's header — should read this rather than `model`, which is
        Anthropic-specific.
        """
        return {
            "nemotron": self.nemotron_model,
            "gemini": self.gemini_model,
        }.get(self.provider, self.model)

    @property
    def is_configured(self) -> bool:
        """True when the *selected* provider has its key.

        Per-provider on purpose: a `GEMINI_API_KEY` left in `.env` from a trial
        must not make an unconfigured Claude deployment look ready.
        """
        return bool({
            "nemotron": self.nvidia_api_key,
            "gemini": self.gemini_api_key,
        }.get(self.provider, self.api_key))


@dataclass(frozen=True)
class PaymentSettings:
    """Voice-initiated money transfer.

    Defaults are deliberately restrictive: off, stubbed, and capped low. Money
    should require someone to have opted in explicitly, not merely to have failed
    to opt out.

    Caps are in **paise**, like everything else in the payment path.
    """

    enabled: bool = False
    #: Which PaymentGateway backs the tools. Only "stub" exists until eko payment
    #: API access lands — see agent/payments.py.
    gateway: str = "stub"

    #: Ceiling on any single transfer. 10,000 rupees.
    max_amount_paise: int = 1_000_000
    #: Ceiling on the first transfer to a number never paid before. With no payee
    #: list and arbitrary numbers, this is the main protection against a misheard
    #: digit sending a large amount to a stranger. 500 rupees.
    first_time_cap_paise: int = 50_000
    #: Everything this payer can send in one calendar day. Without it the
    #: per-transaction caps bound each mistake but not the total — ₹500 to fifty
    #: different wrong numbers passes every other check. 25,000 rupees.
    daily_cap_paise: int = 2_500_000
    #: How a payee may be named:
    #:   contact_preferred — dictated numbers allowed, with a digit read-back
    #:   contact_only      — a shared contact card or a known payee, nothing else
    #:   voice_allowed     — dictated numbers, no read-back required
    #: The default asks for the read-back but does not block anyone; a
    #: deployment that cares more about wrong-number risk than convenience
    #: should use contact_only.
    payee_input_mode: str = "contact_preferred"
    #: How long a prepared intent stays confirmable. Short, so a PIN spoken much
    #: later cannot authorise something the user has forgotten about.
    intent_ttl_s: float = 180.0
    #: Wrong PINs before the intent is cancelled and must be restated.
    max_pin_attempts: int = 3

    #: How often to ask eko about transfers it accepted but has not settled.
    #: 60s because a bank transfer resolves in seconds-to-minutes and each
    #: inquiry is one cheap GET — the cost of asking is far below the cost of
    #: a payer not knowing whether their money moved.
    settlement_poll_interval_s: float = 60.0
    #: How long to keep asking before giving up and saying so.
    #:
    #: Deliberately *not* a small number of attempts. An initiated transfer
    #: cannot be recalled — eko has no cancel endpoint, and its refund API is
    #: for already-failed cash transactions with customer OTP — so giving up
    #: early does not stop the money, it only stops us watching it. Six hours of
    #: cheap GETs is the price of being able to tell the payer what actually
    #: happened.
    settlement_give_up_after_s: float = 21_600.0
    #: Accepted PIN lengths, in the order they are tried.
    pin_lengths: tuple[int, ...] = (4, 6)

    #: POC only. The stub gateway's correct PIN. A real gateway verifies against
    #: eko's own systems and ignores this entirely.
    stub_pin: str = "1234"

    #: How the payer authorises: spoken_pin | upi_link | web_link | otp.
    #:
    #: `spoken_pin` is the POC mechanism and the default. It is a knowledge factor
    #: only, so it does not satisfy the RBI Authentication Mechanisms for Digital
    #: Payment Transactions Directions, 2025 — see agent/handoff.py. Anything else
    #: hands authorisation to the payer's own app and requires a signed callback.
    handoff_kind: str = "spoken_pin"
    #: Shared secret for `POST /payments/callback`. Required by every handoff kind
    #: except spoken_pin: an unsigned endpoint that says money moved can be called
    #: by anyone who finds the URL.
    callback_secret: str | None = None
    #: Base URL for a hosted authorisation page, when handoff_kind is web_link.
    handoff_url: str | None = None

    #: Where the append-only audit trail is written. Empty disables it, which is
    #: fine for local work — but a real gateway refuses to start without one.
    #: Real money with no trail is the combination that is actually dangerous.
    audit_path: str = ""

    @classmethod
    def from_env(cls) -> PaymentSettings:
        return cls(
            enabled=_flag("PAYMENTS_ENABLED", False),
            gateway=os.getenv("PAYMENT_GATEWAY", "stub").strip().lower(),
            max_amount_paise=int(os.getenv("PAYMENT_MAX_PAISE", "1000000")),
            first_time_cap_paise=int(os.getenv("PAYMENT_FIRST_TIME_CAP_PAISE", "50000")),
            daily_cap_paise=int(os.getenv("PAYMENT_DAILY_CAP_PAISE", "2500000")),
            payee_input_mode=os.getenv(
                "PAYMENT_PAYEE_INPUT_MODE", "contact_preferred"
            ).strip().lower(),
            intent_ttl_s=float(os.getenv("PAYMENT_INTENT_TTL_S", "180")),
            max_pin_attempts=int(os.getenv("PAYMENT_MAX_PIN_ATTEMPTS", "3")),
            settlement_poll_interval_s=float(
                os.getenv("PAYMENT_SETTLEMENT_POLL_INTERVAL_S", "60")
            ),
            settlement_give_up_after_s=float(
                os.getenv("PAYMENT_SETTLEMENT_GIVE_UP_AFTER_S", "21600")
            ),
            pin_lengths=tuple(
                int(n) for n in _csv("PAYMENT_PIN_LENGTHS", "4,6")
            ),
            stub_pin=os.getenv("PAYMENT_STUB_PIN", "1234"),
            audit_path=os.getenv("PAYMENT_AUDIT_PATH", ""),
            handoff_kind=os.getenv("PAYMENT_HANDOFF_KIND", "spoken_pin").strip().lower(),
            callback_secret=_optional("PAYMENT_CALLBACK_SECRET"),
            handoff_url=_optional("PAYMENT_HANDOFF_URL"),
        )

    @property
    def needs_callback(self) -> bool:
        """True when authorisation happens elsewhere and a rail must report back."""
        return self.enabled and self.handoff_kind != "spoken_pin"

    @property
    def moves_real_money(self) -> bool:
        """False while the stub is in place. Surfaced on /health so nobody has to
        guess whether a demo was real."""
        return self.enabled and self.gateway != "stub"


@dataclass(frozen=True)
class GroupSettings:
    """Behaviour in WhatsApp groups.

    Off by default: groups need an Official Business Account, and a deployment
    without one should not carry the code path at all.
    """

    enabled: bool = False
    #: What summons the bot. Meta sends no structured "mentions" field for Cloud
    #: API groups, so this is matched as a substring of the text.
    wake_words: tuple[str, ...] = ("@eko", "eko bot", "@bot", "एको")
    #: How long a group stays awake after being summoned. Long enough for a
    #: follow-up, short enough that the bot is not quietly listening to a meeting.
    awake_for_s: float = 300.0
    #: Leave this alone. A PIN spoken as a voice note in a group is audible to
    #: every participant and stays there to be replayed — restricting *who* may
    #: pay does not change that, because it is the payer's own PIN being exposed.
    allow_payments: bool = False

    @classmethod
    def from_env(cls) -> GroupSettings:
        return cls(
            enabled=_flag("GROUPS_ENABLED", False),
            wake_words=_csv("GROUP_WAKE_WORDS", "@eko,eko bot,@bot,एको"),
            awake_for_s=float(os.getenv("GROUP_AWAKE_FOR_S", "300")),
            allow_payments=_flag("GROUP_ALLOW_PAYMENTS", False),
        )


@dataclass(frozen=True)
class EkoSettings:
    """eko's ICICI fund-transfer API.

    Written against the real contract:

        POST {base}/ekoicici/v1/request
        headers: developer_key, secret_key, secret_key_timestamp
        body:    { amount, customer_id, initiator_id, pintwin, client_ref_id, … }

    Three groups of settings. **Credentials** are secrets. **Merchant identity**
    (`user_code`, `initiator_id`, and the service/interaction codes) identifies
    which BC is transacting — eko issues these. **Response field paths** say where
    to find each value inside eko's JSON, so a shape that differs by one level is
    an `.env` edit rather than a code change.

    `pintwin` is eko's name for the PIN. It is never configured here — it arrives
    from the user, one transfer at a time.
    """

    # --- credentials ---
    base_url: str | None = None
    developer_key: str | None = None
    #: Used to derive the per-request `secret_key`.
    auth_key: str | None = None
    #: Set only if eko issues a fixed secret instead of the HMAC scheme.
    static_secret_key: str | None = None
    timeout_s: int = 30

    # --- merchant identity ---
    #: The BC/agent code eko issued.
    user_code: str | None = None
    #: **No longer used to build `initiator_id`.** Kept only because removing
    #: a settings field is a bigger change than leaving an unused one — every
    #: transfer, inquiry and lookup now sends the WhatsApp payer's own number
    #: as `initiator_id` instead (see `eko_gateway.py::_initiator_id_for`),
    #: per an explicit instruction from the project owner: this is who is
    #: actually initiating each transaction, not a single fixed merchant
    #: identity. Not required at startup for this reason — see
    #: `missing_fields`.
    initiator_id: str | None = None
    #: eko's `customer_id` — a **fixed** value issued with the merchant
    #: identity, not the recipient's number.
    #:
    #: It reads like a payee field and is not one. `customer_vpa` identifies
    #: the receiving party; `customer_id` sits beside `user_code` and
    #: `initiator_id` as part of who *we* are. Reading it as the payee's mobile
    #: is what made this flow demand a phone number for every transfer and
    #: refuse ordinary UPI IDs like `name@okicici` that carry none.
    customer_id: str | None = None
    service_code: str = "86"
    interaction_type_id: str = "840"
    org_id: str = "1"
    bc: str = "1"
    source: str = "NEWCONNECT"
    version: str = "v2"
    locale: str = "en"
    lang: str = "null"
    #: eko requires a location on every transfer. WhatsApp gives us none unless
    #: the user shares one, so a fixed branch location is configured instead.
    latlong: str = ""
    #: eko echoes the caller's IP back for its own fraud checks.
    source_ip: str = ""
    next_allowed_limit: str = "25000"
    #: Sent as `recipient_name` when the payee could not be named. eko's own
    #: working request carries a non-empty value here, and eko does not return
    #: names (see `eko_gateway.resolve_payee`), so without this every real
    #: transfer would send an empty one.
    recipient_name: str = ""

    # --- endpoints, relative to base_url ---
    transfer_path: str = "/ekoicici/v1/request"
    #: Optional. Without it a payee has no name and the digits must be read back.
    verify_path: str = ""
    spent_today_path: str = ""
    #: eko's Transaction Inquiry — the only way to learn whether an initiated
    #: transfer settled, since eko publishes no webhook.
    #:
    #: **The LIVE endpoint, confirmed directly by eko on 2026-08-14** via a
    #: real worked curl. Getting here took four attempts, each on a different
    #: host or path than the last:
    #:
    #: 1. `eps.eko.in` — guessed from summarising the docs page's on-screen
    #:    text. Wrong: it is eko's documentation *website*, not an API gateway
    #:    — every path there returns HTTP 200 with an HTML page carrying a
    #:    Google Tag Manager script, four calls running.
    #: 2. `api.eko.in` — fell back to the transfer endpoint's host on the
    #:    reasoning that it, at least, behaved like a real gateway (402
    #:    "Authentication parameters missing" on every path, including
    #:    nonsense ones). A defensible fallback, but still a guess — its
    #:    auth-before-routing behaviour meant it could never confirm a path.
    #: 3. `staging.eko.in/ekoapi/v3` — eko sent a working curl against it,
    #:    which confirmed the request *shape* (`tid` as a query parameter, not
    #:    a path segment — see below) but turned out to be sandbox-only: a
    #:    real production TID came back "not found" here, and eko confirmed
    #:    this host never sees production data.
    #: 4. This value — `api.eko.in/ekoicici/v3/tools/reference/transaction`,
    #:    eko's own worked curl against the live environment. Same host as
    #:    the transfer endpoint, different path (`ekoicici/v3`, not the
    #:    transfer's `ekoicici/v1`) and different again from the sandbox's
    #:    `ekoapi/v3`.
    #:
    #: `GET {inquiry_base_url}{inquiry_path}?tid={tid}&initiator_id=…` — `tid`
    #: is a **query parameter**, not a path segment; confirmed against the
    #: sandbox host in attempt 3 above and carried forward here. Set
    #: `inquiry_path` to "" to switch settlement polling off entirely.
    inquiry_base_url: str = "https://api.eko.in/ekoicici/v3"
    inquiry_path: str = "/tools/reference/transaction"
    #: **No longer used.** eko's own worked sandbox curl used `9911572989`
    #: here as a fixed value, which is what this field held. Superseded by
    #: the same per-payer `initiator_id` change as the transfer path — see
    #: `EkoSettings.initiator_id` — so `inquire()` now derives it from
    #: whichever payer the settlement belongs to instead of reading this.
    #: Kept only so nothing that still references the setting by name breaks.
    inquiry_initiator_id: str = "9911572989"
    #: eko's docs confirm the inquiry endpoint wants `secret-key` /
    #: `secret-key-timestamp` (hyphenated) where the transfer endpoint takes
    #: `secret_key` / `secret_key_timestamp` (underscored). Both are sent
    #: either way — an extra unread header is harmless, a missing one is a 401.
    inquiry_hyphenated_headers: bool = True

    # --- where things live in eko's responses (dotted paths) ---
    field_status: str = "status"
    field_message: str = "message"
    field_reference: str = "data.tid"
    field_payee_name: str = "data.recipient_name"
    field_payee_vpa: str = "data.customer_vpa"
    field_payee_known: str = "data.is_verified"
    field_spent_today: str = "data.used_limit"
    #: Status values that mean eko **accepted the request**. Not the same thing
    #: as the money arriving — see `field_tx_status` below.
    success_statuses: tuple[str, ...] = ("0", "success")

    #: Where the *money's* outcome lives, as opposed to the API call's.
    #:
    #: These are two different questions and eko answers them in two different
    #: fields. `status: 0` only means "request accepted"; a real transfer came
    #: back with `status: 0`, `message: "Transaction Initiated"` and
    #: `tx_status: "2"`, was reported to the sender as done, and then reversed.
    #:
    #: eko's full documented table (eps.eko.in/docs/transaction-inquiry):
    #: `0` success · `1` fail · `2` response awaited/initiated · `3` refund
    #: pending · `4` refunded · `5` hold (inquiry required). Their own rule:
    #: "a transaction must not be treated as declined/failed" on a timeout —
    #: inquire instead of retrying, which is exactly what `agent/settlement.py`
    #: does. `4` (refunded) counts as failed here: the money went out and came
    #: back, so from the payer's side the transfer did not happen. `2`, `3`
    #: and `5` are not listed in either tuple, so they fall through to
    #: "pending" by default — no separate list needed for them.
    field_tx_status: str = "data.tx_status"
    settled_tx_statuses: tuple[str, ...] = ("0",)
    failed_tx_statuses: tuple[str, ...] = ("1", "4")

    # --- reporting: same envelope, different codes ---
    #: eko dispatches on one endpoint and selects the operation with codes, so the
    #: reporting path defaults to the transfer path. Every code and field below is
    #: a guess until a real reporting curl arrives — correct these rather than
    #: editing `eko_source.py`.
    report_path: str = "/ekoicici/v1/request"
    report_interaction_type_id: str = ""
    report_count_code: str = ""
    report_delivery_code: str = ""
    report_list_code: str = ""
    field_req_date: str = "report_date"
    field_req_campaign: str = "campaign_name"
    field_campaign_total: str = "data.total_campaigns"
    field_campaign_split: str = "data.by_campaign"
    field_campaign_list: str = "data.campaigns"
    field_list_item_name: str = "name"
    field_campaign_name: str = "data.campaign_name"
    field_sent: str = "data.sent"
    field_delivered: str = "data.delivered"
    field_failed: str = "data.failed"
    field_pending: str = "data.pending"

    intent_ttl_s: float = 180.0

    @classmethod
    def from_env(cls) -> EkoSettings:
        return cls(
            base_url=_optional("EKO_BASE_URL"),
            developer_key=_optional("EKO_DEVELOPER_KEY"),
            auth_key=_optional("EKO_AUTH_KEY"),
            static_secret_key=_optional("EKO_STATIC_SECRET_KEY"),
            timeout_s=int(os.getenv("EKO_TIMEOUT_S", "30")),
            user_code=_optional("EKO_USER_CODE"),
            initiator_id=_optional("EKO_INITIATOR_ID"),
            customer_id=_optional("EKO_CUSTOMER_ID"),
            service_code=os.getenv("EKO_SERVICE_CODE", "86"),
            interaction_type_id=os.getenv("EKO_INTERACTION_TYPE_ID", "840"),
            org_id=os.getenv("EKO_ORG_ID", "1"),
            bc=os.getenv("EKO_BC", "1"),
            source=os.getenv("EKO_SOURCE", "NEWCONNECT"),
            version=os.getenv("EKO_VERSION", "v2"),
            locale=os.getenv("EKO_LOCALE", "en"),
            lang=os.getenv("EKO_LANG", "null"),
            latlong=os.getenv("EKO_LATLONG", ""),
            source_ip=os.getenv("EKO_SOURCE_IP", ""),
            next_allowed_limit=os.getenv("EKO_NEXT_ALLOWED_LIMIT", "25000"),
            recipient_name=os.getenv("EKO_RECIPIENT_NAME", ""),
            transfer_path=os.getenv("EKO_TRANSFER_PATH", "/ekoicici/v1/request"),
            verify_path=os.getenv("EKO_VERIFY_PATH", ""),
            spent_today_path=os.getenv("EKO_SPENT_TODAY_PATH", ""),
            inquiry_base_url=os.getenv(
                "EKO_INQUIRY_BASE_URL", "https://api.eko.in/ekoicici/v3"
            ),
            inquiry_path=os.getenv("EKO_INQUIRY_PATH", "/tools/reference/transaction"),
            inquiry_initiator_id=os.getenv("EKO_INQUIRY_INITIATOR_ID", "9911572989"),
            inquiry_hyphenated_headers=_flag("EKO_INQUIRY_HYPHENATED_HEADERS", True),
            field_status=os.getenv("EKO_FIELD_STATUS", "status"),
            field_message=os.getenv("EKO_FIELD_MESSAGE", "message"),
            field_reference=os.getenv("EKO_FIELD_REFERENCE", "data.tid"),
            field_payee_name=os.getenv("EKO_FIELD_PAYEE_NAME", "data.recipient_name"),
            field_payee_vpa=os.getenv("EKO_FIELD_PAYEE_VPA", "data.customer_vpa"),
            field_payee_known=os.getenv("EKO_FIELD_PAYEE_KNOWN", "data.is_verified"),
            field_spent_today=os.getenv("EKO_FIELD_SPENT_TODAY", "data.used_limit"),
            success_statuses=_csv("EKO_SUCCESS_STATUSES", "0,success"),
            field_tx_status=os.getenv("EKO_FIELD_TX_STATUS", "data.tx_status"),
            settled_tx_statuses=_csv("EKO_SETTLED_TX_STATUSES", "0"),
            failed_tx_statuses=_csv("EKO_FAILED_TX_STATUSES", "1,4"),
            report_path=os.getenv("EKO_REPORT_PATH", "/ekoicici/v1/request"),
            report_interaction_type_id=os.getenv("EKO_REPORT_INTERACTION_TYPE_ID", ""),
            report_count_code=os.getenv("EKO_REPORT_COUNT_CODE", ""),
            report_delivery_code=os.getenv("EKO_REPORT_DELIVERY_CODE", ""),
            report_list_code=os.getenv("EKO_REPORT_LIST_CODE", ""),
            field_req_date=os.getenv("EKO_REQ_DATE", "report_date"),
            field_req_campaign=os.getenv("EKO_REQ_CAMPAIGN", "campaign_name"),
            field_campaign_total=os.getenv("EKO_FIELD_CAMPAIGN_TOTAL", "data.total_campaigns"),
            field_campaign_split=os.getenv("EKO_FIELD_CAMPAIGN_SPLIT", "data.by_campaign"),
            field_campaign_list=os.getenv("EKO_FIELD_CAMPAIGN_LIST", "data.campaigns"),
            field_list_item_name=os.getenv("EKO_FIELD_LIST_ITEM_NAME", "name"),
            field_campaign_name=os.getenv("EKO_FIELD_CAMPAIGN_NAME", "data.campaign_name"),
            field_sent=os.getenv("EKO_FIELD_SENT", "data.sent"),
            field_delivered=os.getenv("EKO_FIELD_DELIVERED", "data.delivered"),
            field_failed=os.getenv("EKO_FIELD_FAILED", "data.failed"),
            field_pending=os.getenv("EKO_FIELD_PENDING", "data.pending"),
            intent_ttl_s=float(os.getenv("PAYMENT_INTENT_TTL_S", "180")),
        )

    def missing_fields(self, *, for_transfers: bool = False) -> list[str]:
        """What still has to be filled in before this can transact.

        Returned as a list rather than a bool so the error names the gap — a
        deployment failing with "set EKO_USER_CODE" is fixable, one failing with
        "not configured" is a guessing game.

        `EKO_DEVELOPER_KEY`, `EKO_AUTH_KEY` and `EKO_STATIC_SECRET_KEY` are
        deliberately not required here — eko's platform team confirmed the
        `developer_key`/`secret_key`/`secret_key_timestamp` headers must always
        be *present*, but an empty value is fine. See `eko_auth_headers`.

        `EKO_INITIATOR_ID` is also not required — `initiator_id` is now
        derived per-payer at call time (`eko_gateway.py::_initiator_id_for`),
        not read from settings, so there is nothing here to validate.

        `EKO_USER_CODE` and `EKO_CUSTOMER_ID` are also not required, per an
        explicit instruction from the project owner (2026-08-15): both vary
        with `initiator_id` and there is currently no source to derive the
        right value per payer, so empty values are sent deliberately — to
        find out whether eko's API accepts them the same way it accepts an
        empty `developer_key`/`auth_key`/`secret_key`, rather than refusing
        to try. A missing `EKO_CUSTOMER_ID` used to be required only
        `for_transfers`; that parameter is kept on the signature for callers
        that still pass it, but no longer changes what is checked.
        """
        return [
            name
            for name, value in {
                "EKO_BASE_URL": self.base_url,
                # eko rejects a transfer with no location.
                "EKO_LATLONG": self.latlong,
            }.items()
            if not value
        ]

    def missing_report_fields(self) -> list[str]:
        """What the reporting source still needs.

        The credentials are shared with the transfer API, so what is missing here
        is only the codes that select a report — which nobody can guess.
        """
        missing = self.missing_fields()
        # latlong is a transfer requirement; a report does not move money.
        missing = [name for name in missing if name != "EKO_LATLONG"]
        for name, value in {
            "EKO_REPORT_COUNT_CODE": self.report_count_code,
            "EKO_REPORT_DELIVERY_CODE": self.report_delivery_code,
        }.items():
            if not value:
                missing.append(name)
        return missing

    @property
    def is_configured(self) -> bool:
        return not self.missing_fields()


@dataclass(frozen=True)
class DemoSettings:
    """The demo HTTP API and its React UI (not part of the product).

    Off by default because the demo endpoints bypass the WhatsApp allowlist —
    an HTTP caller has no verified phone number to check against it.
    """

    enabled: bool = False
    #: Optional shared secret, sent as `X-Demo-Token`. Recommended whenever the
    #: demo is reachable by anyone but you.
    token: str | None = None
    #: Browser origins allowed to call the demo API.
    #:
    #: A range rather than one port, because Vite silently moves to the next free
    #: one when 5173 is taken — a second `npm run dev`, or one left running in
    #: another terminal. The browser then loads fine and every request fails CORS,
    #: which surfaces as "backend unreachable" and sends you looking at the
    #: server. Allowing the handful of ports Vite actually uses costs nothing:
    #: these are localhost origins on a demo API that is off by default.
    allowed_origins: tuple[str, ...] = tuple(
        f"http://{host}:{port}"
        for port in (5173, 5174, 5175, 5176)
        for host in ("localhost", "127.0.0.1")
    )

    @classmethod
    def from_env(cls) -> DemoSettings:
        return cls(
            enabled=_flag("DEMO_ENABLED", False),
            token=_optional("DEMO_TOKEN"),
            allowed_origins=_csv(
                "DEMO_ALLOWED_ORIGINS",
                ",".join(
                    f"http://{host}:{port}"
                    for port in (5173, 5174, 5175, 5176)
                    for host in ("localhost", "127.0.0.1")
                ),
            ),
        )


@dataclass(frozen=True)
class OpsSettings:
    """The `/ops` activity page — who called what API, and when.

    Off by default, and even when on, every route refuses to answer without
    `OPS_TOKEN` matching. Unlike `/demo`, this reads *real* traffic — real
    phone numbers, real payment attempts — so there is no equivalent of demo's
    "unauthenticated is fine, it's all fake data anyway" fallback.
    """

    enabled: bool = False
    token: str | None = None
    #: Recent events returned per source (app log, payment audit trail) on each
    #: request. Bounded so one browser tab left open cannot make every poll
    #: re-read an ever-growing audit file.
    tail_limit: int = 300

    @classmethod
    def from_env(cls) -> OpsSettings:
        return cls(
            enabled=_flag("OPS_ENABLED", False),
            token=_optional("OPS_TOKEN"),
            tail_limit=int(os.getenv("OPS_TAIL_LIMIT", "300")),
        )


@dataclass(frozen=True)
class Settings:
    """Root settings object. Later phases extend this with their own sections."""

    stt: SttSettings = field(default_factory=SttSettings)
    whatsapp: WhatsAppSettings = field(default_factory=WhatsAppSettings)
    agent: AgentSettings = field(default_factory=AgentSettings)
    payments: PaymentSettings = field(default_factory=PaymentSettings)
    groups: GroupSettings = field(default_factory=GroupSettings)
    eko: EkoSettings = field(default_factory=EkoSettings)
    demo: DemoSettings = field(default_factory=DemoSettings)
    ops: OpsSettings = field(default_factory=OpsSettings)
    #: Shared state for a deployment with more than one worker. Empty means
    #: in-process, which is correct for one worker and only one.
    redis_url: str | None = None
    #: How many uvicorn workers are expected. Read here purely so startup can
    #: refuse the combination of several workers and no shared state.
    workers: int = 1
    data_dir: Path = PROJECT_ROOT / "data"
    log_level: str = "INFO"
    #: "text" for a terminal, "json" for a hosting platform's log aggregator.
    log_format: str = "text"
    #: Which behaviour the server runs — "echo" (Phase 2), "transcribe"
    #: (Phase 3) or "agent" (Phase 4). Config rather than a CLI flag so
    #: `uvicorn --reload`, which re-imports the app factory in a fresh process,
    #: keeps the same behaviour.
    handler: str = "echo"

    @classmethod
    def load(cls, env_file: Path | None = None) -> Settings:
        """Read .env (if present) and build settings.

        Real environment variables win over .env, so CI and one-off overrides
        like `WHISPER_MODEL=tiny python ...` behave as expected.
        """
        load_dotenv(env_file or PROJECT_ROOT / ".env", override=False)
        return cls(
            stt=SttSettings.from_env(),
            whatsapp=WhatsAppSettings.from_env(),
            agent=AgentSettings.from_env(),
            payments=PaymentSettings.from_env(),
            groups=GroupSettings.from_env(),
            eko=EkoSettings.from_env(),
            demo=DemoSettings.from_env(),
            ops=OpsSettings.from_env(),
            redis_url=_optional("REDIS_URL"),
            workers=int(os.getenv("WEB_CONCURRENCY", "1")),
            data_dir=Path(os.getenv("DATA_DIR", str(PROJECT_ROOT / "data"))),
            handler=os.getenv("HANDLER", "echo").strip().lower(),
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
            log_format=os.getenv("LOG_FORMAT", "text").strip().lower(),
        )
