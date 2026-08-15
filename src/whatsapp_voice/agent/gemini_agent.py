"""Google Gemini agent, over Gemini's OpenAI-compatible endpoint.

No new SDK: Google publishes an OpenAI-dialect endpoint alongside its native
one, so this reuses the same `openai` client and the same tool loop as
`NemotronAgent` — see `openai_compatible.py`. Only the three values below
differ.

### ⚠️ The free tier reads your traffic

On the **unpaid** Gemini API, Google uses submitted content and generated
responses to improve its products, and human reviewers may read and annotate
them. Google's own guidance is not to send personal or confidential data to the
unpaid service. Its paid tier does not do this, and neither does Vertex AI.
(EEA/Switzerland/UK are the exception — paid data terms apply there even on the
free tier.)

For this project that is not an abstract worry, because what reaches the model
is exactly the sensitive part: **payee phone numbers, UPI IDs, transfer amounts
and payee names**, all of a real, identifiable person.

One thing it is *not* exposed to, by construction: **the PIN.** The gate in
`payment_flow.py` runs in front of the agent, so a PIN never enters the model's
context on any provider. That property is what makes a free-tier trial merely
questionable rather than immediately disqualifying — but the rest of the
payment detail still goes over the wire.

So: fine for development, wiring, and demos on fake numbers. Not something to
leave switched on for real transfers or a compliance review without reading
those terms first. `AGENT_PROVIDER` makes switching back one line.

Terms: https://ai.google.dev/gemini-api/terms
"""

from __future__ import annotations

from ..config import AgentSettings
from .openai_compatible import OpenAICompatibleAgent


class GeminiAgent(OpenAICompatibleAgent):
    key_setting_name = "GEMINI_API_KEY"
    setup_doc = "docs/phase-4-agent.md"

    @staticmethod
    def api_key_from(settings: AgentSettings) -> str | None:
        return settings.gemini_api_key

    @staticmethod
    def base_url_from(settings: AgentSettings) -> str:
        return settings.gemini_base_url

    @staticmethod
    def model_from(settings: AgentSettings) -> str:
        return settings.gemini_model
