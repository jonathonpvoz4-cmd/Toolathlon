#!/usr/bin/env python3
"""Read Notion one-time login codes out of a NetEase 163 mailbox over IMAP.

Notion mails the sign-in code to whatever address the account is registered
with.  When that address is a 163 mailbox there is no browser session to drive
and no Microsoft security challenge to clear -- the code can simply be read
from IMAP, which is both faster and far more reliable than the web UI (163
serves a slider captcha to headless browsers).

NetEase gates IMAP behind an RFC 2971 ``ID`` command that ``imaplib`` does not
implement: ``LOGIN`` succeeds, then ``SELECT`` fails with
``Unsafe Login. Please contact kefu@188.com for help``.  ``imapclient`` ships
``id_()`` natively, which is why it is used here instead of the standard
library.

Authentication uses the mailbox *authorization code*, not the account
password: 163 rejects the password outright for IMAP.  The code is generated
under Settings -> POP3/SMTP/IMAP.
"""

from __future__ import annotations

import email
import re
from email.header import decode_header, make_header
from email.message import Message
from pathlib import Path
from typing import Iterable

from bs4 import BeautifulSoup
from imapclient import IMAPClient, exceptions as imap_exceptions
from imaplib import IMAP4

_IMAP_HOST = "imap.163.com"
_IMAP_PORT = 993
_IMAP_TIMEOUT_SECONDS = 60

# NetEase requires the client to identify itself before SELECT.  The fields are
# free-form, but `name` / `version` / `vendor` are the ones its filter reads.
_ID_PARAMETERS = {
    "name": "toolathlon-notion-autoauth",
    "version": "1.0.0",
    "vendor": "toolathlon",
    "contact": "toolathlon",
}

# Notion mails the sign-in code to whatever address the account is registered
# with, and the wording follows the mailbox's language.  Every code seen in
# real mail from this deployment is exactly six characters:
#
#   142875   six digits    -- Chinese registration mail (updates.notion.com)
#   VZxHhU   mixed case    -- English login mail        (updates.notion.so)
#
# A four-hyphenated-word shape (`lance-star-grid-tamer`) is described in public
# write-ups but was never observed here, and accepting it produced a false
# positive on ordinary prose -- the footer reads "the all-in-one-workspace",
# whose `one-workspace` matched that pattern.  It is therefore not accepted.
_IS_NOTION_WORD = re.compile(r"^[Nn]otion$")

_SIX_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z0-9]{6})(?![A-Za-z0-9])")

# Only mail from Notion can carry a Notion code, and the check removes unrelated
# senders (marketing, security alerts) from the scan entirely.  Both TLDs are
# live: registration mail arrives from `updates.notion.com` and login codes
# from `updates.notion.so`.
_NOTION_SENDER_DOMAINS = ("notion.com", "notion.so")

# Only these mailboxes can plausibly carry the code; checking them keeps the
# scan small on a busy account.
_CANDIDATE_FOLDERS = ("INBOX", "INBOX.Sent", "Sent", "Sent Messages")

# How many of the newest messages per folder to inspect.
_MAX_MESSAGES = 25

# MIME subtypes that carry readable text.
_TEXT_SUBTYPES = ("plain", "html")


def _is_code_shaped(token: str) -> bool:
    """True when a six-character token has the shape of a Notion code.

    Codes are random over [A-Za-z0-9], so every case mixture occurs -- all
    uppercase (`CNVMUU`), mixed (`VZxHhU`) and all digits (`142875`) have all
    been received.  Requiring mixed case specifically rejected `CNVMUU`, so the
    test is simply "contains an uppercase letter or a digit".

    That separates codes from the prose they sit among: ordinary words in the
    body (`anyone`, `notify`, `notion`) are lowercase, and the brand word is
    excluded by name as well since it is capitalised and would otherwise match.
    """
    if _IS_NOTION_WORD.match(token):
        return False
    if len(token) != 6 or not token.isalnum():
        return False
    return any(c.isupper() for c in token) or any(c.isdigit() for c in token)


def extract_notion_codes(text: str) -> list[str]:
    """Return the Notion code(s) present in one already-flattened body.

    Only the body is scanned.  Headers and the subject carry the brand word and
    the sender address ("notify@updates.notion.com"), whose six-letter tokens
    would otherwise be reported as codes.

    Callers that need the code's *arrival* rather than its text should diff
    against a baseline; this function only reports what is present.
    """
    found: list[str] = []
    for match in _SIX_TOKEN_RE.finditer(text):
        token = match.group(1)
        if _is_code_shaped(token) and token not in found:
            found.append(token)
    return found


class Mailbox163Error(RuntimeError):
    """The 163 mailbox could not be read."""


def _decode_header_value(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return raw


def _html_to_text(html: str) -> str:
    try:
        return BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    except Exception:
        return re.sub(r"<[^>]+>", " ", html)


def _part_text(part: Message) -> str:
    """Decode a single MIME part to text, tolerating broken charsets."""
    payload = part.get_payload(decode=True)
    if payload is None:
        raw = part.get_payload()
        return raw if isinstance(raw, str) else ""
    charset = part.get_content_charset() or "utf-8"
    try:
        text = payload.decode(charset, errors="replace")
    except LookupError:
        text = payload.decode("utf-8", errors="replace")
    if part.get_content_subtype() == "html":
        return _html_to_text(text)
    return text


def message_body(message: Message) -> str:
    """Return only the decoded body text -- no subject, no headers.

    Code extraction runs on this.  The subject and the From header both carry
    the brand word and the sender address, whose six-letter tokens would
    otherwise be mistaken for codes.
    """
    chunks: list[str] = []
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_maintype() != "text":
                continue
            if part.get_content_subtype() not in _TEXT_SUBTYPES:
                continue
            # Nested multiparts repeat the same payload; only leaf parts carry it.
            if part.is_multipart():
                continue
            chunks.append(_part_text(part))
    else:
        chunks.append(_part_text(message))
    return "\n".join(part for part in chunks if part)


def message_text(message: Message) -> str:
    """Flatten a message's subject, sender and body into one searchable string.

    Used for diagnostics and for confirming a mail came from Notion; code
    extraction uses `message_body` instead.
    """
    chunks = [
        _decode_header_value(message.get("Subject")),
        _decode_header_value(message.get("From")),
    ]
    body = message_body(message)
    if body:
        chunks.append(body)
    return "\n".join(part for part in chunks if part)


def sender_domains(message: Message) -> list[str]:
    """Return the domains in the From header, lowercased."""
    raw = message.get("From") or ""
    return [d.lower() for d in re.findall(r"@([A-Za-z0-9.-]+)", raw)]


class Mailbox163:
    """A thin IMAP reader that yields the Notion codes currently visible."""

    def __init__(self, address: str, auth_code: str) -> None:
        self.address = address
        self.auth_code = auth_code
        self._client: IMAPClient | None = None

    def __enter__(self) -> "Mailbox163":
        self.connect()
        return self

    def __exit__(self, *_exc_info) -> None:
        self.close()

    def connect(self) -> None:
        try:
            client = IMAPClient(
                _IMAP_HOST, port=_IMAP_PORT, ssl=True, timeout=_IMAP_TIMEOUT_SECONDS
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as Mailbox163Error
            raise Mailbox163Error(
                f"Could not reach {_IMAP_HOST}:{_IMAP_PORT}: {exc}"
            ) from exc
        try:
            client.login(self.address, self.auth_code)
        except imap_exceptions.LoginError as exc:
            client.shutdown()
            raise Mailbox163Error(
                "163 rejected the IMAP login; the authorization code is wrong or "
                "IMAP is not enabled in the mailbox settings"
            ) from exc
        except Exception as exc:  # noqa: BLE001
            client.shutdown()
            raise Mailbox163Error(f"163 IMAP login failed: {exc}") from exc

        # Must be sent before SELECT, or NetEase answers SELECT with
        # "Unsafe Login".  A mailbox that does not advertise ID rejects the
        # command cleanly, which is harmless -- so failure here is not fatal.
        try:
            client.id_(_ID_PARAMETERS)
        except Exception:  # noqa: BLE001 - optional extension
            pass
        self._client = client

    def close(self) -> None:
        if self._client is None:
            return
        try:
            self._client.logout()
        except Exception:  # noqa: BLE001 - best effort
            pass
        finally:
            self._client = None

    def _folders(self) -> Iterable[str]:
        try:
            existing = {
                item[-1].decode(errors="replace")
                if isinstance(item[-1], bytes)
                else str(item[-1])
                for item in self._client.list_folders()
            }
        except Exception:  # noqa: BLE001 - fall back to the common names
            return _CANDIDATE_FOLDERS
        return [name for name in _CANDIDATE_FOLDERS if name in existing] or ["INBOX"]

    def _message_at(self, uid) -> list[str]:
        """Return the codes carried by one message, or [] if it is not Notion's."""
        try:
            fetched = self._client.fetch([uid], ["RFC822"])
        except Exception:  # noqa: BLE001 - a single broken message is skipped
            return []
        payload = fetched.get(uid, {}).get(b"RFC822")
        if not payload:
            return []
        message = email.message_from_bytes(payload)
        if not any(
            domain == suffix or domain.endswith("." + suffix)
            for domain in sender_domains(message)
            for suffix in _NOTION_SENDER_DOMAINS
        ):
            return []
        return extract_notion_codes(message_body(message))

    def newest_notion_codes(self) -> list[str]:
        """Codes from the single most recent Notion message, or [].

        Used when the caller needs "the code that is current", not a history:
        an older code typed into the login form is a confusing failure, so the
        scan stops at the first Notion mail rather than collecting them all.
        """
        if self._client is None:
            raise Mailbox163Error("The mailbox is not connected")
        for folder in self._folders():
            try:
                self._client.select_folder(folder, readonly=True)
                uids = self._client.search(["ALL"])
            except Exception:  # noqa: BLE001 - a missing folder is not fatal
                continue
            for uid in reversed(sorted(uids)[-_MAX_MESSAGES:]):
                codes = self._message_at(uid)
                if codes:
                    return codes
        return []

    def fetch_notion_codes(self) -> list[str]:
        """Return codes from the newest message in each folder, newest first.

        Callers diff this against a baseline to spot a newly arrived code, so
        the ordering matters.  Reading only the newest message per folder keeps
        a stale code from resurfacing when the scan window shifts between
        passes, which would look like an arrival.
        """
        codes: list[str] = []
        for folder in self._folders():
            try:
                self._client.select_folder(folder, readonly=True)
                uids = self._client.search(["ALL"])
            except Exception:  # noqa: BLE001 - a missing folder is not fatal
                continue
            for uid in reversed(sorted(uids)[-_MAX_MESSAGES:]):
                found = self._message_at(uid)
                if found:
                    codes.extend(found)
                    break
        return codes


def _redact(text: str) -> str:
    return re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[EMAIL]", text)


def main() -> int:
    """Connect to 163 IMAP and report whether any Notion codes are visible."""
    import argparse
    from dotenv import dotenv_values

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path("configs/.mcp-auth/notion.env"),
    )
    args = parser.parse_args()

    values = {k: v or "" for k, v in dotenv_values(args.env_file).items()}
    address = values.get("NETEASE_163_ADDRESS") or values.get("NOTION_ACCOUNT_EMAIL")
    auth_code = values.get("NETEASE_163_AUTH_CODE")
    if not address or not auth_code:
        print(
            "mailbox_163=failed: NETEASE_163_ADDRESS and NETEASE_163_AUTH_CODE "
            "are both required"
        )
        return 1

    try:
        with Mailbox163(address, auth_code) as mailbox:
            codes = mailbox.fetch_notion_codes()
    except Mailbox163Error as exc:
        print(f"mailbox_163=failed: {_redact(str(exc))}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"mailbox_163=failed: {_redact(str(exc))}")
        return 1

    print(f"mailbox_163=ok (notion codes visible: {len(codes)})")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
