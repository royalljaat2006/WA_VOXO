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
    nemotron_model: str = "nvidia/nemotron-3-ultra-550b-a55b"
    nvidia_base_url: str = "https://integrate.api.nvidia.com/v1"
    #: Low, because picking the wrong tool or date is the failure that matters
    #: here — variety is worth nothing. NVIDIA only; Claude rejects this.
    temperature: float = 0.2

    # --- shared ---
    max_tokens: int = 1024
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
                "NEMOTRON_MODEL", "nvidia/nemotron-3-ultra-550b-a55b"
            ),
            nvidia_base_url=os.getenv(
                "NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1"
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
        return self.nemotron_model if self.provider == "nemotron" else self.model

    @property
    def is_configured(self) -> bool:
        """True when the *selected* provider has its key."""
        if self.provider == "nemotron":
            return bool(self.nvidia_api_key)
        return bool(self.api_key)


@dataclass(frozen=True)
class AuthSettings:
    """Who may use the bot (Phase 5).

    Defaults to enabled with an empty allowlist, which denies everyone. That is
    the safe failure: forgetting to configure auth locks the bot down rather than
    opening it up.
    """

    enabled: bool = True
    #: "number:role,number:role". A bare number gets `default_role`.
    allowed_numbers: str = ""
    #: Path to a file in the same "number:role" syntax, one entry per line
    #: (comments after '#' allowed). When set, this is authoritative and
    #: `allowed_numbers` above is ignored — see `FileBackedAccessPolicy`. Lets
    #: a team that turns over often add/remove a number without a redeploy.
    allowed_numbers_file: str = ""
    default_role: str = "viewer"
    #: Messages per caller per window.
    rate_limit: int = 20
    rate_limit_window_s: float = 3600.0

    @classmethod
    def from_env(cls) -> AuthSettings:
        return cls(
            enabled=_flag("AUTH_ENABLED", True),
            allowed_numbers=os.getenv("WHATSAPP_ALLOWED_NUMBERS", ""),
            allowed_numbers_file=os.getenv("WHATSAPP_ALLOWED_NUMBERS_FILE", ""),
            default_role=os.getenv("AUTH_DEFAULT_ROLE", "viewer").strip().lower(),
            rate_limit=int(os.getenv("AUTH_RATE_LIMIT", "20")),
            rate_limit_window_s=float(os.getenv("AUTH_RATE_LIMIT_WINDOW_S", "3600")),
        )


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
    #: The initiating mobile registered with eko.
    initiator_id: str | None = None
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
    #: Whether to send `customer_vpa` alongside the mobile number.
    send_vpa: bool = False

    # --- endpoints, relative to base_url ---
    transfer_path: str = "/ekoicici/v1/request"
    #: Optional. Without it a payee has no name and the digits must be read back.
    verify_path: str = ""
    spent_today_path: str = ""

    # --- where things live in eko's responses (dotted paths) ---
    field_status: str = "status"
    field_message: str = "message"
    field_reference: str = "data.tid"
    field_payee_name: str = "data.recipient_name"
    field_payee_vpa: str = "data.customer_vpa"
    field_payee_known: str = "data.is_verified"
    field_spent_today: str = "data.used_limit"
    #: Status values that mean the transfer went through.
    success_statuses: tuple[str, ...] = ("0", "success")

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
            send_vpa=_flag("EKO_SEND_VPA", False),
            transfer_path=os.getenv("EKO_TRANSFER_PATH", "/ekoicici/v1/request"),
            verify_path=os.getenv("EKO_VERIFY_PATH", ""),
            spent_today_path=os.getenv("EKO_SPENT_TODAY_PATH", ""),
            field_status=os.getenv("EKO_FIELD_STATUS", "status"),
            field_message=os.getenv("EKO_FIELD_MESSAGE", "message"),
            field_reference=os.getenv("EKO_FIELD_REFERENCE", "data.tid"),
            field_payee_name=os.getenv("EKO_FIELD_PAYEE_NAME", "data.recipient_name"),
            field_payee_vpa=os.getenv("EKO_FIELD_PAYEE_VPA", "data.customer_vpa"),
            field_payee_known=os.getenv("EKO_FIELD_PAYEE_KNOWN", "data.is_verified"),
            field_spent_today=os.getenv("EKO_FIELD_SPENT_TODAY", "data.used_limit"),
            success_statuses=_csv("EKO_SUCCESS_STATUSES", "0,success"),
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

    def missing_fields(self) -> list[str]:
        """What still has to be filled in before this can transact.

        Returned as a list rather than a bool so the error names the gap — a
        deployment failing with "set EKO_USER_CODE" is fixable, one failing with
        "not configured" is a guessing game.

        `EKO_DEVELOPER_KEY`, `EKO_AUTH_KEY` and `EKO_STATIC_SECRET_KEY` are
        deliberately not required here — eko's platform team confirmed the
        `developer_key`/`secret_key`/`secret_key_timestamp` headers must always
        be *present*, but an empty value is fine. See `eko_auth_headers`.
        """
        required = {
            "EKO_BASE_URL": self.base_url,
            "EKO_USER_CODE": self.user_code,
            "EKO_INITIATOR_ID": self.initiator_id,
            # eko rejects a transfer with no location.
            "EKO_LATLONG": self.latlong,
        }
        return [name for name, value in required.items() if not value]

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
class Settings:
    """Root settings object. Later phases extend this with their own sections."""

    stt: SttSettings = field(default_factory=SttSettings)
    whatsapp: WhatsAppSettings = field(default_factory=WhatsAppSettings)
    agent: AgentSettings = field(default_factory=AgentSettings)
    auth: AuthSettings = field(default_factory=AuthSettings)
    payments: PaymentSettings = field(default_factory=PaymentSettings)
    groups: GroupSettings = field(default_factory=GroupSettings)
    eko: EkoSettings = field(default_factory=EkoSettings)
    demo: DemoSettings = field(default_factory=DemoSettings)
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
            auth=AuthSettings.from_env(),
            payments=PaymentSettings.from_env(),
            groups=GroupSettings.from_env(),
            eko=EkoSettings.from_env(),
            demo=DemoSettings.from_env(),
            redis_url=_optional("REDIS_URL"),
            workers=int(os.getenv("WEB_CONCURRENCY", "1")),
            data_dir=Path(os.getenv("DATA_DIR", str(PROJECT_ROOT / "data"))),
            handler=os.getenv("HANDLER", "echo").strip().lower(),
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
            log_format=os.getenv("LOG_FORMAT", "text").strip().lower(),
        )
