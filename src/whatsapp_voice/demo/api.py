"""A demo-only HTTP API that runs the pipeline without WhatsApp.

Why this exists: the real path needs Meta credentials, a public HTTPS URL, and a
phone. That is three blockers between "the code works" and "someone can see it
work". This router runs the same transcriber and the same agent over a plain HTTP
call, so the system can be demonstrated today.

It also returns per-stage timings, which WhatsApp cannot show — useful for
explaining where the seconds actually go.

⚠️ An HTTP caller here has no verified phone number at all — every session gets a
synthetic `demo:<session_id>` identity (see `_become_demo_caller`), never a real
one. It is therefore OFF by default, refuses to serve unless `DEMO_ENABLED=true`,
and supports a shared token. Never enable it on a deployment that reaches real data.
"""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile, status
from pydantic import BaseModel, Field

from .. import audio as audio_utils
from ..agent import Agent, AgentError
from ..agent.payment_flow import PaymentFlow
from ..agent.payments import ContactBook, current_payer
from ..config import Settings
from ..stt import Transcriber, TranscriptionError

logger = logging.getLogger(__name__)


def _payer_for(session_id: str) -> str:
    """A demo session's stand-in for a verified phone number.

    Prefixed so it can never collide with a real one, and so a stray intent from
    a demo cannot be confirmed from WhatsApp or the reverse.
    """
    return f"demo:{session_id}"


def _become_demo_caller(session_id: str) -> str:
    """Establish who this request is from."""
    payer = _payer_for(session_id)
    current_payer.set(payer)
    return payer


# ------------------------------------------------------------------ schemas

class Stage(BaseModel):
    """One step of the pipeline, for the trace panel in the UI."""

    name: str
    ms: int
    detail: str = ""


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    #: Scopes conversation history, so follow-ups work per browser session.
    session_id: str = Field(default="demo", max_length=64)


class ShareContactRequest(BaseModel):
    """The demo's stand-in for attaching a WhatsApp contact card.

    On WhatsApp the number arrives from the platform already verified. Here it
    is typed, so this is a *simulation* of that path — it exercises the same
    ContactBook and the same read-back suppression, but the digits themselves
    have not been verified by anyone.
    """

    phone: str = Field(min_length=6, max_length=20)
    name: str | None = Field(default=None, max_length=100)
    session_id: str = Field(default="demo", max_length=64)


class AnswerResponse(BaseModel):
    transcript: str | None = None
    answer: str
    tools_used: list[str] = []
    stages: list[Stage] = []
    input_tokens: int = 0
    output_tokens: int = 0
    #: True when the numbers in `answer` came from the deterministic stub.
    stub_data: bool = True
    refused: bool = False
    #: True when a transfer is prepared and waiting for a PIN. The UI uses it to
    #: show the confirmation card and to stop displaying what the user says next.
    awaiting_pin: bool = False


class AuditEntry(BaseModel):
    """One line of the audit trail, safe to put on a screen.

    Phone numbers are masked here even though the file holds them in full: a
    dashboard is watched by a room, an audit file is read by an auditor. Same
    data, different audience, different rules.
    """

    ts: str
    event: str
    intent_id: str | None = None
    payer: str | None = None
    payee: str | None = None
    payee_name: str | None = None
    payee_source: str | None = None
    amount_paise: int | None = None
    reference: str | None = None
    reason: str | None = None
    status: str | None = None


class MissionControl(BaseModel):
    """What is running, and what it has done — the two halves of point 6."""

    audit_enabled: bool
    total_events: int
    transfers_completed: int
    transfers_failed: int
    total_paise: int
    recent: list[AuditEntry] = []


class DemoStatus(BaseModel):
    handler: str
    speech_engine: str
    model: str
    stub_data: bool
    #: Whether the payment flow is wired up at all, and whether it is the stub.
    payments_enabled: bool = False
    real_money: bool = False


# ------------------------------------------------------------------ plumbing

@dataclass
class DemoDeps:
    """Built once at app startup; the transcriber holds a loaded model."""

    settings: Settings
    transcriber: Transcriber | None = None
    agent: Agent | None = None
    #: The same PaymentFlow the WhatsApp handler uses, so the demo cannot drift
    #: into a second, weaker version of the PIN gate. None when payments are off.
    flow: PaymentFlow | None = None
    #: Shared with the tools, so a contact "shared" here is the same one
    #: resolve_payee will find.
    contacts: ContactBook | None = None
    _warmed: bool = field(default=False, repr=False)

    def warmup(self) -> None:
        if self._warmed:
            return
        if self.transcriber is not None:
            self.transcriber.warmup()
        if self.agent is not None:
            self.agent.warmup()
        self._warmed = True


def _mask_entry(entry: AuditEntry) -> AuditEntry:
    """Last four digits only. Enough to correlate, not enough to publish."""
    return entry.model_copy(update={
        "payer": _mask_number(entry.payer),
        "payee": _mask_number(entry.payee),
    })


def _mask_number(value: str | None) -> str | None:
    if not value:
        return value
    return f"…{value[-4:]}" if len(value) > 4 else value


def _timer():
    start = time.perf_counter()
    return lambda: int((time.perf_counter() - start) * 1000)


def create_demo_router(deps: DemoDeps) -> APIRouter:
    router = APIRouter(prefix="/demo", tags=["demo"])
    demo_settings = deps.settings.demo

    def require_enabled(x_demo_token: str | None = Header(default=None)) -> None:
        if not demo_settings.enabled:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Demo API is disabled. Set DEMO_ENABLED=true to use it.",
            )
        if demo_settings.token and x_demo_token != demo_settings.token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing or wrong X-Demo-Token.",
            )

    @router.get("/status", response_model=DemoStatus,
                dependencies=[Depends(require_enabled)])
    def demo_status() -> DemoStatus:
        """What the UI should show in its header — no secrets in here."""
        s = deps.settings
        return DemoStatus(
            handler=s.handler,
            speech_engine=(
                deps.transcriber.name if deps.transcriber else "not configured"
            ),
            model=s.agent.active_model if deps.agent else "not configured",
            stub_data=s.agent.data_source == "stub",
            payments_enabled=deps.flow is not None,
            real_money=s.payments.moves_real_money,
        )

    @router.get("/audit", response_model=MissionControl,
                dependencies=[Depends(require_enabled)])
    def mission_control(limit: int = 40) -> MissionControl:
        """The review/audit half of mission control.

        Reads the trail rather than keeping a parallel in-memory copy, so what
        the screen shows is what was actually recorded — a dashboard fed from
        somewhere other than the evidence is a dashboard that can disagree with
        it.
        """
        path = deps.settings.payments.audit_path
        if not path or not Path(path).exists():
            return MissionControl(
                audit_enabled=bool(path), total_events=0,
                transfers_completed=0, transfers_failed=0, total_paise=0,
            )

        entries: list[AuditEntry] = []
        completed = failed = total_paise = 0

        for line in Path(path).read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue  # a torn final line is expected on an append-only file
            if record.get("event") == "transfer_executed":
                completed += 1
                total_paise += int(record.get("amount_paise") or 0)
            elif record.get("event") in ("transfer_failed", "transfer_refused"):
                failed += 1
            entries.append(AuditEntry(**{
                key: value for key, value in record.items()
                if key in AuditEntry.model_fields
            }))

        return MissionControl(
            audit_enabled=True,
            total_events=len(entries),
            transfers_completed=completed,
            transfers_failed=failed,
            total_paise=total_paise,
            recent=[_mask_entry(entry) for entry in entries[-limit:][::-1]],
        )

    @router.post("/ask", response_model=AnswerResponse,
                 dependencies=[Depends(require_enabled)])
    def ask(request: AskRequest) -> AnswerResponse:
        """Text question → answer. The typed path, skipping speech entirely."""
        if deps.agent is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Agent is not configured — set ANTHROPIC_API_KEY and HANDLER=agent.",
            )

        payer = _become_demo_caller(request.session_id)

        # The payment gate runs before the model, exactly as it does on
        # WhatsApp. Without this the demo would forward a typed PIN straight
        # into the model's context.
        if deps.flow is not None:
            elapsed = _timer()
            outcome = deps.flow.handle(payer, request.question)
            if outcome.reply is not None:
                return AnswerResponse(
                    answer=outcome.reply,
                    stages=[Stage(name="payment", ms=elapsed(), detail="PIN verified locally")],
                    stub_data=deps.settings.agent.data_source == "stub",
                )

        elapsed = _timer()
        try:
            reply = deps.agent.answer(request.session_id, request.question)
        except AgentError as exc:
            logger.warning("Demo agent call failed: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"The agent could not answer: {exc}",
            ) from exc

        return AnswerResponse(
            answer=reply.text,
            tools_used=list(reply.tools_used),
            stages=[Stage(name="agent", ms=elapsed(),
                          detail=deps.settings.agent.active_model)],
            input_tokens=reply.input_tokens,
            output_tokens=reply.output_tokens,
            stub_data=deps.settings.agent.data_source == "stub",
            refused=reply.refused,
            awaiting_pin=deps.flow is not None and deps.flow.awaiting_pin(payer),
        )

    @router.post("/contact", response_model=AnswerResponse,
                 dependencies=[Depends(require_enabled)])
    def share_contact(request: ShareContactRequest) -> AnswerResponse:
        """Simulate attaching a WhatsApp contact card.

        Records the number as contact-shared and tells the agent about it, so
        the rest of the conversation ("2000 bhej do") resolves against a payee
        that needs no digit read-back — the same path a real card takes.
        """
        if deps.contacts is None or deps.agent is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Payments are not enabled — set PAYMENTS_ENABLED=true and HANDLER=payment.",
            )

        payer = _become_demo_caller(request.session_id)
        deps.contacts.remember(payer, request.phone, request.name)

        described = request.name or request.phone
        elapsed = _timer()
        try:
            reply = deps.agent.answer(
                request.session_id,
                f"[The user shared a contact card: {described}, number {request.phone}. "
                "Use this number as the payee. Ask how much they want to send.]",
            )
        except AgentError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"The agent could not answer: {exc}",
            ) from exc

        return AnswerResponse(
            answer=reply.text,
            tools_used=list(reply.tools_used),
            stages=[Stage(name="contact", ms=elapsed(), detail="digits verified, no ASR")],
            input_tokens=reply.input_tokens,
            output_tokens=reply.output_tokens,
            stub_data=deps.settings.agent.data_source == "stub",
            awaiting_pin=deps.flow is not None and deps.flow.awaiting_pin(payer),
        )

    @router.post("/voice", response_model=AnswerResponse,
                 dependencies=[Depends(require_enabled)])
    async def voice(
        audio: UploadFile = File(...),
        session_id: str = Form(default="demo"),
    ) -> AnswerResponse:
        """Recorded audio → transcript → answer, with per-stage timings.

        This is the demo's centrepiece: the same path a real voice note takes,
        minus Meta.
        """
        if deps.transcriber is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="No speech engine configured.",
            )

        stages: list[Stage] = []
        workdir = Path(tempfile.mkdtemp(prefix="demo-voice-"))
        try:
            # 1. Receive — the demo's stand-in for Meta's media download.
            elapsed = _timer()
            suffix = Path(audio.filename or "recording.webm").suffix or ".webm"
            source = workdir / f"recording{suffix}"
            source.write_bytes(await audio.read())
            stages.append(Stage(name="upload", ms=elapsed(),
                                detail=f"{source.stat().st_size // 1024} KB {suffix}"))

            # 2. Convert to the 16kHz mono WAV every engine agrees on.
            elapsed = _timer()
            try:
                wav = audio_utils.to_wav(source, workdir)
            except audio_utils.AudioError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=f"That audio could not be converted: {exc}",
                ) from exc
            stages.append(Stage(name="convert", ms=elapsed(),
                                detail=f"{suffix} → 16kHz mono wav"))

            # 3. Transcribe.
            elapsed = _timer()
            try:
                transcript = deps.transcriber.transcribe(wav)
            except TranscriptionError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=f"Could not transcribe: {exc}",
                ) from exc
            stages.append(Stage(name="transcribe", ms=elapsed(),
                                detail=deps.transcriber.name))
        finally:
            # Recorded audio is customer data even in a demo.
            shutil.rmtree(workdir, ignore_errors=True)

        if not transcript.text:
            return AnswerResponse(
                transcript="",
                answer="I didn't hear anything in that recording. Try again?",
                stages=stages,
                stub_data=deps.settings.agent.data_source == "stub",
            )

        payer = _become_demo_caller(session_id)

        # 4. The payment gate, before the model and before the transcript is
        #    shown. A voice note spoken while a transfer is pending is very
        #    likely to be the PIN, and returning it would put it on screen.
        was_awaiting = deps.flow is not None and deps.flow.awaiting_pin(payer)
        if deps.flow is not None:
            elapsed = _timer()
            outcome = deps.flow.handle(payer, transcript.text)
            if outcome.reply is not None:
                stages.append(
                    Stage(name="payment", ms=elapsed(), detail="PIN verified locally")
                )
                return AnswerResponse(
                    transcript=None if was_awaiting else transcript.text,
                    answer=outcome.reply,
                    stages=stages,
                    stub_data=deps.settings.agent.data_source == "stub",
                )

        # 5. Answer. Without an agent, still show the transcript — that alone
        #    demonstrates Phases 0 and 3.
        if deps.agent is None:
            return AnswerResponse(
                transcript=transcript.text,
                answer=f"🎤 {transcript.text}",
                stages=stages,
                stub_data=deps.settings.agent.data_source == "stub",
            )

        elapsed = _timer()
        try:
            reply = deps.agent.answer(session_id, transcript.text,
                                      language=transcript.language)
        except AgentError as exc:
            logger.warning("Demo agent call failed: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Heard “{transcript.text}” but the agent could not answer: {exc}",
            ) from exc
        stages.append(Stage(name="agent", ms=elapsed(),
                            detail=deps.settings.agent.active_model))

        return AnswerResponse(
            transcript=transcript.text,
            answer=reply.text,
            tools_used=list(reply.tools_used),
            stages=stages,
            input_tokens=reply.input_tokens,
            output_tokens=reply.output_tokens,
            stub_data=deps.settings.agent.data_source == "stub",
            refused=reply.refused,
            awaiting_pin=deps.flow is not None and deps.flow.awaiting_pin(payer),
        )

    return router
