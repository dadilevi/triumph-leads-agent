"""How messages leave the system.

  wame       phase-1: nothing is sent by the app; a human clicks a wa.me link and presses send.
  simulator  messages are only stored; the /simulator page plays the customer. For testing.
  cloud      WhatsApp Business Cloud API (official). Requires a Meta app + phone number id + token.

Never add a channel that automates WhatsApp Web (see SPEC / CLAUDE.md).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import urllib.error
import urllib.request
from dataclasses import dataclass


class SendError(RuntimeError):
    pass


@dataclass
class SendResult:
    wa_id: str | None = None


class Channel:
    name = "base"
    can_send = False  # False → a human must send (wa.me)
    needs_template_for_first_message = False

    def send_text(self, to: str, text: str) -> SendResult:  # pragma: no cover
        raise NotImplementedError

    def send_template(self, to: str, template: str, lang: str, params: list[str]) -> SendResult:  # pragma: no cover
        raise NotImplementedError


class WameChannel(Channel):
    name = "wame"
    can_send = False


class SimulatorChannel(Channel):
    """Accepts everything; the engine stores the message and the simulator page shows it."""

    name = "simulator"
    can_send = True

    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def send_text(self, to: str, text: str) -> SendResult:
        self.sent.append((to, text))
        return SendResult()

    def send_template(self, to: str, template: str, lang: str, params: list[str]) -> SendResult:
        self.sent.append((to, f"[template:{template}] {' | '.join(params)}"))
        return SendResult()


class CloudChannel(Channel):
    """WhatsApp Business Cloud API — POST /{phone_number_id}/messages."""

    name = "cloud"
    can_send = True
    needs_template_for_first_message = True

    def __init__(self, phone_number_id: str, token: str, api_version: str = "v21.0", opener=None):
        if not phone_number_id or not token:
            raise ValueError("WHATSAPP_PHONE_NUMBER_ID and WHATSAPP_TOKEN are required for CHANNEL=cloud")
        self.url = f"https://graph.facebook.com/{api_version}/{phone_number_id}/messages"
        self.token = token
        self._open = opener or urllib.request.urlopen

    def _post(self, payload: dict) -> SendResult:
        req = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._open(req, timeout=15) as resp:
                body = json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:500]
            raise SendError(f"WhatsApp API {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise SendError(f"WhatsApp API unreachable: {e.reason}") from e
        msgs = body.get("messages") or [{}]
        return SendResult(wa_id=msgs[0].get("id"))

    def send_text(self, to: str, text: str) -> SendResult:
        return self._post({"messaging_product": "whatsapp", "to": to, "type": "text",
                           "text": {"body": text, "preview_url": False}})

    def send_template(self, to: str, template: str, lang: str, params: list[str]) -> SendResult:
        comp = [{"type": "body", "parameters": [{"type": "text", "text": p} for p in params]}] if params else []
        return self._post({"messaging_product": "whatsapp", "to": to, "type": "template",
                           "template": {"name": template, "language": {"code": lang}, "components": comp}})


def verify_signature(app_secret: str, body: bytes, header: str | None) -> bool:
    """Meta signs webhook POSTs with X-Hub-Signature-256: sha256=<hmac(app_secret, body)>."""
    if not app_secret:
        return False
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.split("=", 1)[1])


@dataclass
class InboundEvent:
    kind: str  # "customer" (message from a lead) | "echo" (business sent from the WhatsApp Business app)
    phone: str  # the customer's number, digits only
    text: str
    wa_id: str
    profile_name: str = ""


def parse_webhook(payload: dict) -> list[InboundEvent]:
    """Extract customer messages and coexistence echoes from a Cloud API webhook payload.

    Non-text messages (images, voice...) become a placeholder text so the agent can hand off.
    Status updates (sent/delivered/read) are ignored.
    """
    out: list[InboundEvent] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            field = change.get("field")
            value = change.get("value", {})
            if field == "messages":
                names = {c.get("wa_id"): (c.get("profile") or {}).get("name", "") for c in value.get("contacts", [])}
                for m in value.get("messages", []):
                    out.append(InboundEvent("customer", m.get("from", ""), _text_of(m), m.get("id", ""),
                                            names.get(m.get("from"), "")))
            elif field == "smb_message_echoes":  # coexistence: a human replied from the phone app
                for m in value.get("message_echoes", []):
                    out.append(InboundEvent("echo", m.get("to", ""), _text_of(m), m.get("id", "")))
    return out


def _text_of(m: dict) -> str:
    t = m.get("type")
    if t == "text":
        return (m.get("text") or {}).get("body", "")
    if t == "button":
        return (m.get("button") or {}).get("text", "")
    if t == "interactive":
        i = m.get("interactive") or {}
        return (i.get("button_reply") or i.get("list_reply") or {}).get("title", "")
    labels = {"image": "תמונה", "audio": "הודעה קולית", "video": "וידאו", "document": "מסמך",
              "sticker": "סטיקר", "location": "מיקום", "contacts": "איש קשר"}
    return f"[{labels.get(t, t or 'הודעה')} — לא נתמך]"
