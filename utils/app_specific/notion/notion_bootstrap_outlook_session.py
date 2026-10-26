#!/usr/bin/env python3
"""One-time bootstrap for a trusted Outlook browser session.

`notion_oauth_autoauth.py` can drive every step of the Microsoft login except
the security challenge: when Microsoft flags the sign-in as unfamiliar it mails
a one-time code to the account's *recovery* mailbox, which this repository has
no access to.  That challenge only appears for untrusted browsers, and the
resulting trust is what `notion_oauth_autoauth.py` persists as
`outlook_state.json`.

Run this once, read the code out of the recovery mailbox, paste it here.  The
saved state makes every later auto-auth attempt look like the same device, so
the challenge is not raised again.

Usage:
    uv run python utils/app_specific/notion/notion_bootstrap_outlook_session.py
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from utils.app_specific.notion import notion_oauth_autoauth as auth  # noqa: E402


DEFAULT_AUTH_DIR = auth.DEFAULT_AUTH_DIR
CODE_PROMPT_SECONDS = 20 * 60


def _challenge_is_up(page) -> bool:
    return auth._is_visible(page.locator("#iProof0"), timeout=1_000)


def _request_recovery_code(page, recovery_email: str) -> None:
    """Drive the security challenge up to the point where the code is mailed.

    The challenge page shows the recovery address masked, and Microsoft will not
    send anything until the full address is re-entered in the `iProofEmail`
    field it reveals.  "I have a code" is a separate link that skips straight to
    the code-entry form without mailing anything, so it must not be used here.
    """
    radio = page.locator("#iProof0").first
    radio.wait_for(state="visible", timeout=20_000)
    if not radio.is_checked():
        radio.check()
    submit = page.locator("#iSelectProofAction").first
    if auth._is_visible(submit, timeout=3_000):
        submit.click()
    else:
        auth._click_named(page, (re.compile(r"^send code$", re.I),))
    page.wait_for_timeout(4_000)

    if auth._is_visible(page.locator("#iProofEmail"), timeout=10_000):
        page.locator("#iProofEmail").first.fill(recovery_email)
        page.wait_for_timeout(1_500)
        if not auth._click_named(page, (re.compile(r"send code", re.I),)):
            raise auth.AutoAuthError("The 'Send code' control was not found")
        page.wait_for_timeout(5_000)

    if not auth._is_visible(page.locator("#iOttText"), timeout=20_000):
        raise auth.AutoAuthError(
            "Microsoft did not advance to the security-code entry form after "
            "requesting a recovery code"
        )


def _submit_code(page, code: str) -> None:
    """Enter the mailed code on the 'Enter your security code' form."""
    field = auth._first_visible(
        page,
        ("#iOttText", "input[name=iOttText]", "input[autocomplete=one-time-code]"),
        20_000,
    )
    field.fill(code.strip())
    if not auth._click_named(
        page, (re.compile(r"verify|next|continue|submit|sign in", re.I),)
    ):
        auth._first_visible(
            page, ("#iVerifyCodeAction", "input[type=submit]"), 5_000
        ).click()


def _read_code_from_stdin(deadline: float) -> str:
    print()
    print(">>> Waiting for the code from the recovery mailbox.")
    print(f">>> Paste it below ({(deadline - time.monotonic()) / 60:.0f} minutes to go).")
    while time.monotonic() < deadline:
        try:
            code = input("recovery code: ").strip()
        except EOFError:
            raise auth.AutoAuthError("No recovery code was provided on stdin")
        if code:
            return code
    raise auth.AutoAuthError("Timed out waiting for the recovery code")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--auth-dir", type=Path, default=DEFAULT_AUTH_DIR)
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args()

    auth_dir = args.auth_dir.resolve()
    env_file = (args.env_file or auth_dir / "notion.env").resolve()
    state_path = auth_dir / "outlook_state.json"

    with auth.sync_playwright() as playwright:
        browser = auth._launch_chromium(playwright)
        try:
            authorizer = auth.BrowserAuthorizer(browser, auth_dir, env_file)
            credentials = authorizer._load_credentials()
            context = authorizer._new_context()
            page = context.new_page()
            page.goto(
                "https://login.live.com/",
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            auth._first_visible(
                page,
                ("#usernameEntry", "input[name=loginfmt]", "#i0116", "input[type=email]"),
                15_000,
            ).fill(credentials["NOTION_ACCOUNT_EMAIL"])
            if not auth._click_named(page, (re.compile(r"next|continue", re.I),)):
                auth._first_visible(
                    page, ("button[data-testid=primaryButton]", "input[type=submit]"), 5_000
                ).click()
            page.wait_for_timeout(2_500)

            auth._click_named(
                page,
                (
                    re.compile(r"use your password", re.I),
                    re.compile(r"sign in with a password", re.I),
                ),
            )
            page.wait_for_timeout(2_500)
            auth._first_visible(
                page,
                ("#passwordEntry", "input[name=passwd]", "#i0118", "input[type=password]"),
                20_000,
            ).fill(credentials["OUTLOOK_ACCOUNT_PASSWORD"])
            if not auth._click_named(page, (re.compile(r"next|sign in|continue", re.I),)):
                auth._first_visible(
                    page, ("button[data-testid=primaryButton]", "input[type=submit]"), 5_000
                ).click()
            page.wait_for_timeout(4_000)

            if _challenge_is_up(page):
                _request_recovery_code(page, credentials["OUTLOOK_RECOVERY_EMAIL"])
                print()
                print(">>> A one-time code has been mailed to the recovery mailbox.")
                print(">>> Open it, find the newest Microsoft code, paste it below.")
                code = _read_code_from_stdin(time.monotonic() + CODE_PROMPT_SECONDS)
                _submit_code(page, code)
                page.wait_for_timeout(6_000)
                if _challenge_is_up(page):
                    raise auth.AutoAuthError(
                        "The recovery code was rejected; the challenge is still up"
                    )

            page.goto(
                "https://outlook.live.com/mail/0/",
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            page.wait_for_timeout(6_000)
            if not auth._mailbox_is_open(page):
                raise auth.AutoAuthError(
                    "Signed in, but the Outlook inbox did not open; "
                    f"landed on {auth.redact(page.url)}"
                )

            auth._save_state(context, state_path)
            print(f"outlook_state={state_path} (trusted session saved)")
            context.close()
        finally:
            browser.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except auth.ManualRecoveryRequired as exc:
        print(f"bootstrap=manual_recovery_required: {auth.redact(str(exc))}")
        sys.exit(20)
    except Exception as exc:
        print(f"bootstrap=failed: {auth.redact(str(exc))}")
        sys.exit(1)
