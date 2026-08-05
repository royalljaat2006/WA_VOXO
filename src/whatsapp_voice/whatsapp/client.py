"""A thin client over the WhatsApp Business Cloud API (Graph API).

Only the calls this project needs, each one small and independently testable:

- send_text        — reply to a user (Phase 1 hello, Phase 2 echo, Phase 4 answers)
- get_media_url    — resolve a media_id to a temporary download URL (Phase 3)
- download_media   — fetch the bytes of a voice note (Phase 3)
- create_group …   — the group calls, which have their own rules; see below

Everything goes through the Graph API at
`{graph_base_url}/{api_version}/...` with a bearer token. The client is
deliberately synchronous and dependency-light (just `requests`); the webhook
server in Phase 2 calls it from a background task so the request handler can
return 200 immediately.
"""

from __future__ import annotations

from pathlib import Path

import requests

from ..config import WhatsAppSettings

#: Meta's ceiling for a Cloud API group. Enforced here so the error names the
#: limit rather than arriving as a generic 400 from Graph.
MAX_GROUP_PARTICIPANTS = 8


class WhatsAppError(RuntimeError):
    """A Cloud API call failed. Carries the HTTP status when there was one."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class WhatsAppClient:
    def __init__(self, settings: WhatsAppSettings, session: requests.Session | None = None) -> None:
        if not settings.is_configured:
            raise WhatsAppError(
                "WhatsApp is not configured. Set WHATSAPP_ACCESS_TOKEN and "
                "WHATSAPP_PHONE_NUMBER_ID in .env — see docs/phase-1-meta-setup.md"
            )
        self._settings = settings
        # A shared Session keeps the TLS connection warm across calls and lets
        # tests inject a mock transport.
        self._session = session or requests.Session()
        self._session.headers.update(
            {"Authorization": f"Bearer {settings.access_token}"}
        )

    # ---------------------------------------------------------------- URLs

    @property
    def _base(self) -> str:
        s = self._settings
        return f"{s.graph_base_url}/{s.api_version}"

    def _messages_url(self) -> str:
        return f"{self._base}/{self._settings.phone_number_id}/messages"

    # ------------------------------------------------------------- sending

    def send_text(self, to: str, body: str, preview_url: bool = False) -> str:
        """Send a plain-text message. Returns the WhatsApp message id (wamid).

        `to` is the recipient's phone number in international format without the
        leading '+', e.g. "919876543210".
        """
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "text",
            "text": {"preview_url": preview_url, "body": body},
        }
        data = self._post(self._messages_url(), json=payload)
        try:
            return data["messages"][0]["id"]
        except (KeyError, IndexError) as exc:
            raise WhatsAppError(f"Unexpected send response shape: {data}") from exc

    # -------------------------------------------------------------- groups
    #
    # Meta's Groups API (2026) constrains this more than the 1:1 API does, and
    # the constraints shape the product rather than just the code:
    #
    #   · The business must CREATE the group. It cannot be added to an existing
    #     one, so "add the bot to your team chat" is not a thing that can happen.
    #   · One Cloud API business per group, at most 8 participants.
    #   · Interactive messages (buttons, lists) are not delivered to groups, so
    #     every group confirmation has to be plain text or a link.
    #   · An Official Business Account is required.
    #
    # All four are Meta's, not ours. Where a method would be pointless under
    # them, it is absent rather than present and broken.

    def _groups_url(self, suffix: str = "") -> str:
        return f"{self._base}/{self._settings.phone_number_id}/groups{suffix}"

    def create_group(self, subject: str, participants: list[str]) -> str:
        """Create a group and return its id.

        The business has to be the creator — there is no join. `participants` are
        phone numbers in international format without the '+'.
        """
        if not participants:
            raise WhatsAppError("A group needs at least one participant.")
        if len(participants) > MAX_GROUP_PARTICIPANTS:
            raise WhatsAppError(
                f"WhatsApp allows at most {MAX_GROUP_PARTICIPANTS} participants "
                f"in a Cloud API group; {len(participants)} were given."
            )

        data = self._post(self._groups_url(), json={
            "messaging_product": "whatsapp",
            "subject": subject,
            "participants": [{"user": number} for number in participants],
        })
        group_id = _first_id(data, "groups") or data.get("id")
        if not group_id:
            raise WhatsAppError(f"No group id in create response: {data}")
        return str(group_id)

    def get_group_invite_link(self, group_id: str) -> str:
        """The join link, for anyone who could not be added directly."""
        data = self._get(self._groups_url(f"/{group_id}/invite"))
        link = data.get("invite_link") or data.get("link")
        if not link:
            raise WhatsAppError(f"No invite link in response for {group_id}: {data}")
        return str(link)

    def send_group_text(self, group_id: str, body: str) -> str:
        """Send plain text to a group.

        Separate from `send_text` because the payload differs (`recipient_type`)
        and because buttons are silently undeliverable here — a caller that has
        to think about which one to use is a caller that will not accidentally
        send a group an interactive message that never arrives.
        """
        data = self._post(self._messages_url(), json={
            "messaging_product": "whatsapp",
            "recipient_type": "group",
            "to": group_id,
            "type": "text",
            "text": {"preview_url": False, "body": body},
        })
        try:
            return data["messages"][0]["id"]
        except (KeyError, IndexError) as exc:
            raise WhatsAppError(f"Unexpected send response shape: {data}") from exc

    def remove_group_participant(self, group_id: str, participant: str) -> None:
        self._delete(self._groups_url(f"/{group_id}/participants/{participant}"))

    def leave_group(self, group_id: str) -> None:
        """Delete the group, which is how the business leaves one.

        Meta gives no "leave" for the creator: deleting is the only exit, and it
        removes the group for everyone. Named for what it means to us and
        documented for what it does to them.
        """
        self._delete(self._groups_url(f"/{group_id}"))

    # -------------------------------------------------------------- media

    def get_media_url(self, media_id: str) -> str:
        """Resolve a media_id (from an incoming message) to a download URL.

        The URL is short-lived (~5 minutes) and must be fetched with the bearer
        token, so resolve and download back-to-back rather than storing it.
        """
        data = self._get(f"{self._base}/{media_id}")
        url = data.get("url")
        if not url:
            raise WhatsAppError(f"No 'url' in media metadata for {media_id}: {data}")
        return url

    def download_media(self, media_url: str, dest: Path) -> Path:
        """Download media bytes to `dest`. Requires the bearer token header."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        response = self._session.get(
            media_url, timeout=self._settings.request_timeout_s, stream=True
        )
        self._raise_for_status(response, context=f"downloading {media_url}")
        with dest.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=8192):
                handle.write(chunk)
        return dest

    def fetch_media(self, media_id: str, dest: Path) -> Path:
        """Convenience: media_id → download URL → bytes on disk, in one call.

        This is the two-step dance the docs warn about, done back-to-back so the
        temporary URL never goes stale.
        """
        return self.download_media(self.get_media_url(media_id), dest)

    # ---------------------------------------------------------- transport

    def _post(self, url: str, json: dict) -> dict:
        response = self._session.post(
            url, json=json, timeout=self._settings.request_timeout_s
        )
        self._raise_for_status(response, context=f"POST {url}")
        return response.json()

    def _get(self, url: str) -> dict:
        response = self._session.get(url, timeout=self._settings.request_timeout_s)
        self._raise_for_status(response, context=f"GET {url}")
        return response.json()

    def _delete(self, url: str) -> dict:
        response = self._session.delete(url, timeout=self._settings.request_timeout_s)
        self._raise_for_status(response, context=f"DELETE {url}")
        return response.json() if response.content else {}

    @staticmethod
    def _raise_for_status(response: requests.Response, context: str) -> None:
        if response.ok:
            return
        # Meta returns a structured error body — surface its message, which is
        # far more useful than a bare status code (e.g. expired token, bad id).
        detail = response.text[:500]
        try:
            error = response.json().get("error", {})
            if error:
                detail = f"{error.get('message', '')} (code {error.get('code')})"
        except ValueError:
            pass
        raise WhatsAppError(
            f"{context} failed: HTTP {response.status_code} — {detail}",
            status_code=response.status_code,
        )


def _first_id(data: dict, key: str) -> str | None:
    entries = data.get(key)
    if isinstance(entries, list) and entries and isinstance(entries[0], dict):
        return entries[0].get("id")
    return None
