"""Assemble an agent from settings.

The one place that decides which data source and which model back the agent, so
swapping the stub for eko's real API is a single edit here plus a new
`CampaignDataSource` implementation.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..audit import AuditTrail
from ..config import AgentSettings, EkoSettings, PaymentSettings
from .base import Agent
from .campaign_tools import campaign_tools
from .claude_agent import SYSTEM_PROMPT, ClaudeAgent
from .conversation import ConversationStore
from .datasource import CampaignDataSource, StubCampaignSource
from .payment_tools import PAYMENT_PROMPT, payment_tools
from .payments import ContactBook, PaymentGateway, PendingIntents, StubPaymentGateway
from .tools import Tool, ToolRegistry

logger = logging.getLogger(__name__)


def build_data_source(
    settings: AgentSettings, eko: EkoSettings | None = None
) -> CampaignDataSource:
    """The campaign data source named by `AGENT_DATA_SOURCE`."""
    if settings.data_source == "stub":
        logger.warning(
            "Using STUB campaign data — every number is fake. "
            "Set AGENT_DATA_SOURCE=eko once the reporting API is configured."
        )
        return StubCampaignSource()

    if settings.data_source == "eko":
        from .eko_source import EkoCampaignSource

        logger.info("Campaign data: eko reporting API")
        return EkoCampaignSource(eko or EkoSettings.from_env())

    raise ValueError(
        f"Unknown AGENT_DATA_SOURCE={settings.data_source!r}. "
        "Valid values: 'stub', 'eko'."
    )


def build_audit_trail(settings: PaymentSettings) -> AuditTrail:
    """The audit trail, and the rule that a real gateway cannot run without one.

    Refusing here rather than warning is deliberate: an operator who forgot to
    configure the trail finds out at startup, not when someone asks what happened
    to a transfer three weeks ago.
    """
    if settings.audit_path:
        return AuditTrail(Path(settings.audit_path))

    if settings.gateway != "stub":
        raise ValueError(
            f"PAYMENT_GATEWAY={settings.gateway!r} moves real money, so "
            "PAYMENT_AUDIT_PATH must be set. A payment system with no audit "
            "trail cannot answer the only question that matters afterwards."
        )

    logger.warning("No PAYMENT_AUDIT_PATH — transfers are not being recorded.")
    return AuditTrail(None)


def build_gateway(
    settings: PaymentSettings, eko: EkoSettings | None = None
) -> PaymentGateway:
    """The payment gateway named by `PAYMENT_GATEWAY`."""
    if settings.gateway == "stub":
        logger.warning(
            "Using STUB payment gateway — NO REAL MONEY MOVES. "
            "Every transfer is simulated and the PIN is a fixed test value."
        )
        return StubPaymentGateway(pin=settings.stub_pin, intent_ttl_s=settings.intent_ttl_s)

    if settings.gateway == "eko":
        # Imported here so a stub deployment never loads the module that can
        # move money.
        from .eko_gateway import EkoPaymentGateway

        logger.warning("PAYMENT_GATEWAY=eko — REAL MONEY. Confirm compliance sign-off.")
        return EkoPaymentGateway(eko or EkoSettings.from_env())

    raise ValueError(
        f"Unknown PAYMENT_GATEWAY={settings.gateway!r}. Valid values: 'stub', 'eko'."
    )


def build_agent(
    settings: AgentSettings,
    payments: PaymentSettings | None = None,
    gateway: PaymentGateway | None = None,
    pending: PendingIntents | None = None,
    contacts: ContactBook | None = None,
    audit: AuditTrail | None = None,
    eko: EkoSettings | None = None,
) -> Agent:
    """The agent named by `AGENT_PROVIDER`, wired to tools and a data source.

    Both providers get the same tools and the same conversation store — only the
    wire format differs. Adding a third provider means a new `Agent` subclass
    plus one branch here.

    Payment tools are added only when `payments.enabled`, along with the prompt
    section describing them. A deployment that does not move money therefore
    carries neither the tools nor the instructions, so there is nothing for a
    prompt injection to aim at.
    """
    source = build_data_source(settings, eko)
    tool_list: list[Tool] = list(campaign_tools(source))
    prompt = SYSTEM_PROMPT

    if payments and payments.enabled:
        if gateway is None or pending is None:
            raise ValueError(
                "Payments are enabled but no gateway/pending store was passed. "
                "Use build_payment_stack() rather than calling build_agent() directly."
            )
        tool_list += payment_tools(gateway, pending, payments, contacts, audit)
        prompt += PAYMENT_PROMPT

    tools = ToolRegistry(tool_list)
    conversations = ConversationStore(
        max_turns=settings.max_history_turns,
        idle_timeout_s=settings.conversation_idle_timeout_s,
    )

    if settings.provider == "claude":
        logger.info("Agent: claude model=%s effort=%s tools=%d",
                    settings.model, settings.effort, len(tools))
        return ClaudeAgent(settings, tools, conversations, system_prompt=prompt)

    if settings.provider == "nemotron":
        from .nemotron_agent import NemotronAgent

        logger.info("Agent: nemotron model=%s temperature=%s tools=%d",
                    settings.nemotron_model, settings.temperature, len(tools))
        return NemotronAgent(settings, tools, conversations, system_prompt=prompt)

    raise ValueError(
        f"Unknown AGENT_PROVIDER={settings.provider!r}. "
        "Valid values: 'claude', 'nemotron'."
    )


def build_payment_stack(
    agent_settings: AgentSettings,
    payment_settings: PaymentSettings,
    eko: EkoSettings | None = None,
) -> tuple[Agent, PaymentGateway, PendingIntents, ContactBook, AuditTrail]:
    """Agent, gateway, pending-intent store and contact book, sharing instances.

    They have to be built together. The tools hold the same `PendingIntents` the
    handler reads, or an intent the model prepares is invisible when the PIN
    arrives — and the same `ContactBook` the handler fills, or a shared contact
    is indistinguishable from a dictated number by the time it is resolved.
    """
    gateway = build_gateway(payment_settings, eko)
    audit = build_audit_trail(payment_settings)
    pending = PendingIntents()
    contacts = ContactBook()
    agent = build_agent(
        agent_settings, payment_settings, gateway, pending, contacts, audit, eko
    )
    return agent, gateway, pending, contacts, audit
