"""The webhook server Meta calls when someone messages our number.

Two endpoints, both at the path Meta is configured with:

    GET  /webhook  — one-time verification handshake when you save the config
    POST /webhook  — every incoming message and status callback

Three rules drive the design, all of them learned the hard way:

1. **Always 200, and fast.** Meta retries any non-200 with backoff, so a slow or
   failing handler turns one message into a pile of duplicates. The POST route
   validates, schedules the real work in the background, and returns
   immediately — transcription and API calls take seconds, far longer than a
   webhook should hold a connection.
2. **Verify the signature.** The endpoint is public; see `signature.py`.
3. **Deduplicate.** Retries and at-least-once delivery mean the same
   `message_id` can arrive more than once. Replying twice to one voice note is
   the visible symptom.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Query, Request, Response, status

from ..audit import AuditTrail
from ..config import Settings, WhatsAppSettings
from ..store import KeyValueStore, MemoryStore, build_store, guard_worker_count
from .client import WhatsAppClient
from .handlers import (
    AgentHandler,
    EchoHandler,
    MessageHandler,
    TranscribeHandler,
)
from .payloads import parse_webhook
from .signature import SIGNATURE_HEADER, is_valid_signature

logger = logging.getLogger(__name__)


def build_handler(settings: Settings, store: KeyValueStore | None = None) -> MessageHandler:
    """Pick the handler named by `HANDLER`.

    Each phase adds one line to `_build_inner`. The alternative — an `if` inside
    the webhook route — would mean reopening the transport layer every phase,
    which is what the `MessageHandler` seam exists to avoid.

    No access guard: every number that reaches this server is answered. There is
    no allowlist and no per-number payment permission — anyone who can message
    this WhatsApp number can talk to whichever handler is configured, payments
    included, subject only to the amount caps and PIN in `agent/payment_flow.py`.
    """
    store = store or build_store(settings.redis_url)
    client = WhatsAppClient(settings.whatsapp)
    inner = _build_inner(settings, client, store)

    if settings.groups.enabled:
        from .group_handler import GroupHandler, GroupRegistry

        inner = GroupHandler(
            inner,
            client,
            registry=GroupRegistry(awake_for_s=settings.groups.awake_for_s),
            wake_words=settings.groups.wake_words,
            allow_payments_in_groups=settings.groups.allow_payments,
        )

    return inner


#: Set by `_build_inner` when the payment stack is built, so `create_app` can
#: mount the callback route against the same gateway and pending store the handler
#: uses. A module-level handoff rather than a return-type change, because every
#: other handler branch has nothing to hand back.
_payment_parts: dict[str, object] = {}


def _innermost(handler: MessageHandler) -> MessageHandler:
    """Unwrap every wrapper to the handler that decides the behaviour.

    `/health` answers "what is actually deployed?", and the useful answer is
    `PaymentHandler`, not the name of whichever wrapper happens to be outermost
    (`GroupHandler`, when groups are enabled).
    """
    seen = 0
    while (nested := getattr(handler, "inner", None)) is not None and seen < 10:
        handler = nested
        seen += 1
    return handler


def _audit_trail(settings: Settings) -> AuditTrail:
    """The payment stack's own trail when there is one, otherwise a fresh one.

    Reusing the existing instance matters: `AuditTrail` serialises writes with a
    per-instance lock, so two instances appending to one file could interleave
    and produce a corrupt line — the one thing an evidence file cannot afford.
    """
    existing = _payment_parts.get("audit")
    if isinstance(existing, AuditTrail):
        return existing
    path = settings.payments.audit_path
    return AuditTrail(Path(path)) if path else AuditTrail(None)


def _build_inner(
    settings: Settings, client: WhatsAppClient, store: KeyValueStore | None = None
) -> MessageHandler:
    name = settings.handler

    if name == "echo":
        return EchoHandler(client)

    if name == "transcribe":
        # Imported here so the echo path does not require the STT extras to be
        # installed.
        from ..stt import build_primary_transcriber

        return TranscribeHandler(client, build_primary_transcriber(settings.stt))

    if name == "agent":
        from ..agent import build_agent
        from ..stt import build_primary_transcriber

        return AgentHandler(
            client,
            build_primary_transcriber(settings.stt),
            build_agent(settings.agent),
        )

    if name == "payment":
        from ..agent.factory import build_payment_stack
        from ..stt import build_primary_transcriber
        from .payment_handler import PaymentHandler

        if not settings.payments.enabled:
            raise ValueError(
                "HANDLER=payment but PAYMENTS_ENABLED is not set. Money movement "
                "has to be switched on deliberately — set PAYMENTS_ENABLED=true."
            )

        agent, gateway, pending, contacts, audit = build_payment_stack(
            settings.agent, settings.payments, settings.eko, store, settings.pintwin
        )

        # Only a real gateway can be asked about a transfer, and only when eko
        # has an inquiry endpoint configured. A stub deployment has nothing to
        # poll, so it gets no queue and says so in its reply instead.
        settlements = None
        if not gateway.is_stub and settings.eko.inquiry_path and store is not None:
            from ..agent.settlement import SettlementQueue

            settlements = SettlementQueue(store)

        _payment_parts.update(
            gateway=gateway, pending=pending, audit=audit, settlements=settlements
        )
        return PaymentHandler(
            client,
            build_primary_transcriber(settings.stt),
            agent,
            gateway,
            pending,
            settings.payments,
            contacts,
            audit,
            settlements=settlements,
        )

    raise ValueError(
        f"Unknown HANDLER={name!r}. Valid values: 'echo' (Phase 2), "
        "'transcribe' (Phase 3), 'agent' (Phase 4), 'payment'."
    )


class SeenMessages:
    """Remembers recent message ids so retries don't get processed twice.

    Backed by a `KeyValueStore`, so a deployment with several workers shares one
    set of ids instead of each worker keeping its own and replying again.

    The claim is atomic on purpose. A read-then-write would let two workers both
    find an id absent and both reply — the exact duplicate this exists to
    prevent, and the one that only appears under the load that makes it hardest
    to reproduce.
    """

    #: Comfortably longer than Meta's retry schedule, short enough that ids do
    #: not accumulate for ever.
    TTL_S = 6 * 3600

    def __init__(self, store: KeyValueStore | None = None) -> None:
        # `is not None`, never `or`: an empty MemoryStore is falsy because it
        # defines __len__, so `store or MemoryStore()` silently discards the
        # shared store it was handed and gives every worker its own.
        self._store = store if store is not None else MemoryStore()

    def seen_before(self, message_id: str) -> bool:
        return not self._store.claim(f"seen:{message_id}", ttl_s=self.TTL_S)


def create_app(
    settings: Settings | None = None,
    handler: MessageHandler | None = None,
) -> FastAPI:
    """Build the webhook app.

    `handler` defaults to whatever `HANDLER` names in the environment; tests and
    later phases can pass their own instead (see `handlers.py`).
    """
    settings = settings or Settings.load()
    whatsapp: WhatsAppSettings = settings.whatsapp

    store = build_store(settings.redis_url)
    # Fails at startup with the reason rather than in production as duplicate
    # replies and lost PINs while every health check passes.
    guard_worker_count(settings.workers, store)

    if handler is None:
        handler = build_handler(settings, store)

    # Load models now, not on the first user's message — otherwise a cold start
    # adds tens of seconds to the first reply.
    handler.warmup()

    app = FastAPI(title="WhatsApp Voice Agent", version="0.1.0")
    seen = SeenMessages(store)

    if settings.payments.needs_callback and _payment_parts:
        # Only when authorisation happens in the payer's own app. A spoken-PIN
        # deployment has no rail to call back, so the endpoint does not exist —
        # one less public surface asserting that money moved.
        from .callback import mount_payment_callback

        mount_payment_callback(
            app, settings,
            gateway=_payment_parts["gateway"],      # type: ignore[arg-type]
            pending=_payment_parts["pending"],      # type: ignore[arg-type]
            client=WhatsAppClient(settings.whatsapp),
            audit=_payment_parts["audit"],          # type: ignore[arg-type]
            store=store,
        )
        logger.info("Payment callback mounted at /payments/callback (signed)")

    _start_settlement_poller(settings, store)

    if settings.demo.enabled:
        _mount_demo(app, settings, handler)

    if settings.ops.enabled:
        _mount_ops(app, settings)

    @app.get("/health")
    def health() -> dict[str, object]:
        """Liveness and configuration report.

        Hosting platforms poll this to decide whether the container is up. It also
        answers "what is actually deployed right now?" without shell access —
        which handler, whether the data is real. Deliberately contains no
        secrets and no phone numbers.
        """
        return {
            "status": "ok",
            "handler": type(_innermost(handler)).__name__,
            # There is no allowlist and no per-number payment permission — every
            # number that messages this deployment is answered. See
            # `build_handler`.
            "access_control": "none",
            "signature_verification": bool(whatsapp.app_secret),
            # Every handler that reaches the agent reaches the data source, so
            # this must not be tied to one handler name.
            "stub_data": settings.handler in {"agent", "payment"}
            and settings.agent.data_source == "stub",
            "payments_enabled": settings.payments.enabled,
            # The question someone will actually ask about a deployment: can
            # this thing move money?
            "real_money": settings.payments.moves_real_money,
            "audit_trail": bool(settings.payments.audit_path),
            "groups_enabled": settings.groups.enabled,
            "handoff_kind": settings.payments.handoff_kind,
            "shared_state": type(store).__name__,
        }

    @app.get("/webhook")
    def verify(
        mode: str | None = Query(None, alias="hub.mode"),
        token: str | None = Query(None, alias="hub.verify_token"),
        challenge: str | None = Query(None, alias="hub.challenge"),
    ) -> Response:
        """Meta's one-time handshake: echo back `hub.challenge` as plain text.

        Returning JSON here (FastAPI's default) fails verification — Meta wants
        the raw challenge string and nothing else.
        """
        if not whatsapp.verify_token:
            logger.error("WHATSAPP_VERIFY_TOKEN is not set; cannot verify webhook")
            return Response(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

        if mode == "subscribe" and token == whatsapp.verify_token and challenge:
            logger.info("Webhook verification succeeded")
            return Response(content=challenge, media_type="text/plain")

        logger.warning("Webhook verification rejected (mode=%s)", mode)
        return Response(status_code=status.HTTP_403_FORBIDDEN)

    @app.post("/webhook")
    async def receive(request: Request, background: BackgroundTasks) -> Response:
        raw = await request.body()

        if whatsapp.app_secret:
            signature = request.headers.get(SIGNATURE_HEADER)
            if not is_valid_signature(raw, signature, whatsapp.app_secret):
                logger.warning("Rejected webhook with invalid signature")
                return Response(status_code=status.HTTP_403_FORBIDDEN)
        else:
            # Allowed so Phase 2 can be exercised before the app secret is in
            # .env, but it leaves the endpoint open to anyone who knows the URL.
            logger.warning(
                "WHATSAPP_APP_SECRET is not set — accepting webhook WITHOUT "
                "signature verification. Set it before any real deployment."
            )

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Webhook body was not valid JSON; ignoring")
            return _ok()

        for message in parse_webhook(payload):
            if seen.seen_before(message.message_id):
                logger.info("Skipping duplicate delivery of %s", message.message_id)
                continue
            logger.info("Queued %s from %s", type(message).__name__, message.from_number)
            background.add_task(_run_handler, handler, message)

        # 200 even when nothing matched: status callbacks and unknown shapes are
        # normal traffic, not errors.
        return _ok()

    return app


def _ok() -> Response:
    return Response(status_code=status.HTTP_200_OK)


def _run_handler(handler: MessageHandler, message) -> None:
    """Run a handler, swallowing failures.

    This executes after the 200 has gone out, so raising would only kill the
    background task and log a stack trace — the useful part is the log line.
    """
    try:
        handler.handle(message)
    except Exception:
        logger.exception("Handler failed for message %s", message.message_id)


def _mount_demo(app: FastAPI, settings: Settings, handler: MessageHandler) -> None:
    """Attach the demo API and open CORS for the UI's origin.

    Only called when DEMO_ENABLED is true. Imported lazily so a production
    deployment never loads the demo module at all.

    The transcriber and agent are lifted off the already-warmed handler rather
    than built again — otherwise the speech model would be loaded into memory
    twice.
    """
    from fastapi.middleware.cors import CORSMiddleware

    from ..demo import DemoDeps, create_demo_router

    inner = getattr(handler, "inner", handler)
    deps = DemoDeps(
        settings=settings,
        transcriber=getattr(inner, "_transcriber", None),
        agent=getattr(inner, "_agent", None),
        # Shared, not rebuilt: the demo has to see the same pending intents the
        # tools write, or a PIN typed in the browser would find nothing to
        # confirm.
        flow=getattr(inner, "_flow", None),
        contacts=getattr(inner, "_contacts", None),
    )
    deps.warmup()

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.demo.allowed_origins),
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-Demo-Token"],
    )
    app.include_router(create_demo_router(deps))

    logger.warning(
        "DEMO API ENABLED at /demo — it has no verified phone number behind it "
        "at all (see demo/api.py). Never enable this on a deployment with real "
        "data.%s",
        "" if settings.demo.token else " No DEMO_TOKEN is set, so it is unauthenticated.",
    )


def _start_settlement_poller(settings: Settings, store: KeyValueStore) -> None:
    """Follow transfers eko accepted but has not settled, and tell the payer.

    eko publishes no webhook for the initiated → settled transition, so without
    this the payer's last word on their money is "not confirmed yet" — for
    ever. Started only when there is a real gateway with an inquiry endpoint;
    a stub deployment has nothing to ask about.
    """
    settlements = _payment_parts.get("settlements")
    gateway = _payment_parts.get("gateway")
    if settlements is None or gateway is None:
        return

    from .settlement_poller import SettlementPoller

    poller = SettlementPoller(
        settlements,           # type: ignore[arg-type]
        gateway,
        WhatsAppClient(settings.whatsapp),
        interval_s=settings.payments.settlement_poll_interval_s,
        give_up_after_s=settings.payments.settlement_give_up_after_s,
        audit=_audit_trail(settings),
        store=store,
    )
    poller.start()
    _payment_parts["settlement_poller"] = poller


def _mount_ops(app: FastAPI, settings: Settings) -> None:
    """Attach the token-protected `/ops` activity page.

    Only called when OPS_ENABLED is true. Unlike `_mount_demo`, this reads real
    traffic, so a missing OPS_TOKEN is not a quieter warning — the router itself
    refuses every request until one is set (see `ops_page._require_token`).
    """
    from ..ops_page import create_ops_router

    app.include_router(create_ops_router(settings))

    logger.warning(
        "OPS ACTIVITY PAGE ENABLED at /ops%s",
        "" if settings.ops.token else
        " — NO OPS_TOKEN SET, every request will be refused until one is configured.",
    )
