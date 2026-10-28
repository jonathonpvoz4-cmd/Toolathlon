#!/usr/bin/env python3
"""Recover the hosted Notion MCP OAuth session without human interaction.

The normal access-token refresh remains mcp-remote's responsibility.  This
helper covers the harder case where the refresh token has been revoked: it
drives Notion's OAuth page, reads the one-time login code from whichever
mailbox the account is registered with, and lets mcp-remote persist the
replacement token set.

Two mailboxes are supported and selected by MAILBOX_PROVIDER in the credential
file (or --mailbox):

  outlook  a browser session against outlook.live.com.  Microsoft challenges
           unfamiliar browsers and mails a recovery code to a *separate*
           mailbox, which this script cannot read -- see
           notion_bootstrap_outlook_session.py for the one-time clearance.
  163      IMAP against imap.163.com.  No browser, no challenge, no session to
           expire; strongly preferred when the account is reachable there.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

import yaml
from dotenv import dotenv_values
from playwright.sync_api import (
    Browser,
    BrowserContext,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

# The recovery helper is spawned both as `python <path>` (by
# notion_page_duplicator) and as a module.  When run as a script, sys.path[0]
# is this file's directory rather than the repository root, so `utils` would
# not resolve; adding the root keeps both invocations working.
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from utils.app_specific.notion.notion_mailbox_163 import (  # noqa: E402
    Mailbox163,
    Mailbox163Error,
)

DEFAULT_AUTH_DIR = REPO_ROOT / "configs" / ".mcp-auth"
DEFAULT_SERVER_URL = "https://mcp.notion.com/mcp"
DEFAULT_CALLBACK_PORT = 15348
DEFAULT_TIMEOUT_SECONDS = 240.0
DEFAULT_LOCK_WAIT_SECONDS = 600.0

# Which mailbox carries the Notion sign-in code.  Notion mails it to whatever
# address the account is registered with, so this tracks the account, not a
# preference: switch it when the Notion account's e-mail changes.
MAILBOX_OUTLOOK = "outlook"
MAILBOX_NETEASE_163 = "163"
DEFAULT_MAILBOX = MAILBOX_OUTLOOK

_AUTH_URL_RE = re.compile(r"https://mcp\.notion\.com/authorize\?[^\s]+")
_NOTION_CODE_RE = re.compile(
    r"(?:Login to Notion|登录 Notion)\s+([A-Za-z0-9]{6})"
)
_EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)


class AutoAuthError(RuntimeError):
    """The automatic OAuth recovery could not complete."""


class ManualRecoveryRequired(AutoAuthError):
    """Microsoft requires proof from a separate recovery mailbox."""


def extract_notion_codes(text: str) -> list[str]:
    """Return visible Notion login codes in mailbox display order."""
    return [match.group(1) for match in _NOTION_CODE_RE.finditer(text)]


def extract_authorization_url(text: str) -> str | None:
    """Extract mcp-remote's authorization URL without logging it."""
    match = _AUTH_URL_RE.search(text)
    return match.group(0) if match else None


def redact(text: str) -> str:
    """Remove credentials and transient OAuth material from diagnostics."""
    text = _EMAIL_RE.sub("[EMAIL]", text)
    text = _AUTH_URL_RE.sub("[NOTION_AUTH_URL]", text)
    return re.sub(r"\b[A-Za-z0-9_-]{24,}\b", "[TOKEN]", text)


def _is_visible(locator: Locator, timeout: float = 500.0) -> bool:
    try:
        return locator.first.is_visible(timeout=timeout)
    except PlaywrightTimeoutError:
        return False


def _first_visible(page: Page, selectors_: Iterable[str], timeout: float) -> Locator:
    deadline = time.monotonic() + timeout / 1000
    while time.monotonic() < deadline:
        for selector in selectors_:
            locator = page.locator(selector).first
            if _is_visible(locator):
                return locator
        time.sleep(0.2)
    raise AutoAuthError(f"No expected input became visible within {timeout / 1000:.0f}s")


def _click_named(page: Page, patterns: Iterable[re.Pattern[str]]) -> bool:
    for pattern in patterns:
        for role in ("button", "link"):
            locator = page.get_by_role(role, name=pattern).first
            if _is_visible(locator):
                locator.click()
                return True
    return False


def _body_text(page: Page) -> str:
    return page.locator("body").inner_text(timeout=10_000)


def _mailbox_is_open(page: Page) -> bool:
    text = _body_text(page)
    return any(marker in text for marker in ("Inbox", "收件箱", "New mail", "新邮件"))


def _harden(path: Path) -> None:
    """Best-effort 0600 hardening.

    The auth directory is bind-mounted into task containers that run as root,
    so files written there arrive on the host owned by root.  A host-side run
    as a normal user cannot chmod those; that must not abort the recovery.
    """
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _save_state(context: BrowserContext, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    context.storage_state(path=str(path))
    _harden(path)


class _OutlookCodeReader:
    """Adapts a live Outlook inbox page to the mailbox-reader interface."""

    def __init__(self, context: BrowserContext, page: Page, state_path: Path) -> None:
        self._context = context
        self._page = page
        self._state_path = state_path

    def visible_codes(self) -> list[str]:
        return extract_notion_codes(_body_text(self._page))

    def wait_for_new_code(
        self, baseline: set[str], timeout_seconds: float = 90.0
    ) -> str:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            self._page.reload(wait_until="domcontentloaded", timeout=60_000)
            self._page.wait_for_timeout(1_800)
            for code in self.visible_codes():
                if code not in baseline:
                    return code
            time.sleep(1)
        raise AutoAuthError("No new Notion login code appeared in Outlook")

    def close(self) -> None:
        _save_state(self._context, self._state_path)
        self._context.close()


class _NetEaseCodeReader:
    """Adapts the 163 IMAP mailbox to the same interface as the Outlook reader.

    There is no browser session to preserve here, so `close()` only drops the
    connection.
    """

    def __init__(self, mailbox) -> None:
        self._mailbox = mailbox

    def visible_codes(self) -> list[str]:
        return self._mailbox.fetch_notion_codes()
    def wait_for_new_code(
        self, baseline: set[str], timeout_seconds: float = 90.0
    ) -> str:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            for code in self.visible_codes():
                if code not in baseline:
                    return code
            time.sleep(3)
        raise AutoAuthError("No new Notion login code appeared in the 163 mailbox")

    def close(self) -> None:
        self._mailbox.close()


class BrowserAuthorizer:
    def __init__(
        self,
        browser: Browser,
        auth_dir: Path,
        env_file: Path,
        force_notion_login: bool = False,
        mailbox: str = DEFAULT_MAILBOX,
    ) -> None:
        self.browser = browser
        self.auth_dir = auth_dir
        self.env_file = env_file
        self.force_notion_login = force_notion_login
        self.mailbox = mailbox
        self.outlook_state = auth_dir / "outlook_state.json"
        self.notion_state = auth_dir / "notion_login_state.json"
        self.credentials: dict[str, str] | None = None

    def _load_credentials(self) -> dict[str, str]:
        if self.credentials is not None:
            return self.credentials
        if not self.env_file.is_file():
            raise AutoAuthError(f"Credential file is missing: {self.env_file}")
        values = {
            key: value or ""
            for key, value in dotenv_values(self.env_file).items()
        }
        required = ["NOTION_ACCOUNT_EMAIL"]
        if self.mailbox == MAILBOX_NETEASE_163:
            required += ["NETEASE_163_ADDRESS", "NETEASE_163_AUTH_CODE"]
        else:
            required += ["OUTLOOK_ACCOUNT_PASSWORD", "OUTLOOK_RECOVERY_EMAIL"]
        missing = [key for key in required if not values.get(key)]
        if missing:
            raise AutoAuthError(
                f"Credential file is missing required fields for the "
                f"'{self.mailbox}' mailbox: " + ", ".join(missing)
            )
        self.credentials = values
        return values

    def _new_context(self, state_path: Path | None = None) -> BrowserContext:
        kwargs: dict[str, str] = {"locale": "en-US"}
        if state_path and state_path.is_file():
            kwargs["storage_state"] = str(state_path)
        try:
            return self.browser.new_context(**kwargs)
        except Exception:
            if "storage_state" not in kwargs:
                raise
            return self.browser.new_context(locale="en-US")

    def _open_outlook_reader(self) -> _OutlookCodeReader:
        context, page = self._open_outlook()
        return _OutlookCodeReader(context, page, self.outlook_state)

    def _open_netease_reader(self) -> _NetEaseCodeReader:
        credentials = self._load_credentials()
        mailbox = Mailbox163(
            credentials["NETEASE_163_ADDRESS"],
            credentials["NETEASE_163_AUTH_CODE"],
        )
        try:
            mailbox.connect()
        except Mailbox163Error as exc:
            raise AutoAuthError(f"The 163 mailbox is unusable: {exc}") from exc
        return _NetEaseCodeReader(mailbox)

    def _open_mailbox(self):
        """Return a reader for whichever mailbox carries the Notion code."""
        if self.mailbox == MAILBOX_NETEASE_163:
            return self._open_netease_reader()
        return self._open_outlook_reader()

    def _open_outlook(self) -> tuple[BrowserContext, Page]:
        credentials = self._load_credentials()
        context = self._new_context(self.outlook_state)
        page = context.new_page()
        page.goto(
            "https://outlook.live.com/mail/0/",
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        page.wait_for_timeout(4_000)
        if _mailbox_is_open(page):
            return context, page

        context.close()
        context = self._new_context()
        page = context.new_page()
        page.goto(
            "https://login.live.com/",
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        _first_visible(
            page,
            (
                "#usernameEntry",
                "input[name=loginfmt]",
                "#i0116",
                "input[type=email]",
            ),
            15_000,
        ).fill(credentials["NOTION_ACCOUNT_EMAIL"])
        if not _click_named(page, (re.compile(r"next|continue", re.I),)):
            _first_visible(
                page,
                ("button[data-testid=primaryButton]", "input[type=submit]"),
                5_000,
            ).click()
        page.wait_for_timeout(1_200)

        # Microsoft may interpose a "Verify your email" challenge that offers a
        # backup code to the recovery mailbox.  That page carries no password
        # field, so treat its presence as "switch to the password path" rather
        # than blindly filling whatever password box shows up next.
        if _is_visible(
            page.locator("#proof-confirmation-email-input"), timeout=3_000
        ) or _is_visible(
            page.get_by_text(re.compile(r"verify your email", re.I)).first,
            timeout=1_000,
        ):
            if not _click_named(
                page,
                (
                    re.compile(r"use your password", re.I),
                    re.compile(r"sign in with a password", re.I),
                ),
            ):
                raise ManualRecoveryRequired(
                    "Microsoft demanded recovery-mailbox verification and the "
                    "'Use your password' fallback was not offered"
                )
            page.wait_for_timeout(2_000)

        _first_visible(
            page,
            (
                "#passwordEntry",
                "input[name=passwd]",
                "#i0118",
                "input[type=password]",
            ),
            20_000,
        ).fill(credentials["OUTLOOK_ACCOUNT_PASSWORD"])
        if not _click_named(
            page, (re.compile(r"next|sign in|continue", re.I),)
        ):
            _first_visible(
                page,
                ("button[data-testid=primaryButton]", "input[type=submit]"),
                5_000,
            ).click()
        page.wait_for_timeout(3_000)

        if _is_visible(page.locator("#iProof0"), timeout=2_000):
            raise ManualRecoveryRequired(
                "Microsoft rejected the saved Outlook session and requires a "
                "separate recovery-mailbox security code"
            )

        _click_named(
            page,
            (
                re.compile(r"yes", re.I),
                re.compile(r"stay signed in", re.I),
            ),
        )
        page.goto(
            "https://outlook.live.com/mail/0/",
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        page.wait_for_timeout(4_000)
        if not _mailbox_is_open(page):
            raise AutoAuthError("Outlook login completed but the inbox did not open")
        _save_state(context, self.outlook_state)
        return context, page

    def _log_in_to_notion(self, page: Page) -> None:
        credentials = self._load_credentials()
        mailbox = self._open_mailbox()
        try:
            baseline = set(mailbox.visible_codes())
            _first_visible(
                page,
                ("input[type=email]", 'input[placeholder*="email" i]'),
                20_000,
            ).fill(credentials["NOTION_ACCOUNT_EMAIL"])
            # The Notion password is optional: accounts configured for
            # email-code sign-in never render the password field, and Notion
            # falls straight through to the one-time-code input.
            notion_password = credentials.get("NOTION_ACCOUNT_PASSWORD", "")
            password = page.locator("input[type=password]").first
            if notion_password and _is_visible(password):
                password.fill(notion_password)
            if not _click_named(
                page,
                (
                    re.compile(r"continue", re.I),
                    re.compile(r"log in", re.I),
                    re.compile(r"sign in", re.I),
                ),
            ):
                raise AutoAuthError("Notion login submit control was not found")

            code_input = _first_visible(
                page,
                (
                    "input[autocomplete=one-time-code]",
                    'input[placeholder="Enter code"]',
                    'input[placeholder*="code" i]',
                ),
                30_000,
            )
            code = mailbox.wait_for_new_code(baseline)
            code_input.fill(code)
            if not _click_named(
                page,
                (
                    re.compile(r"continue", re.I),
                    re.compile(r"log in", re.I),
                    re.compile(r"sign in", re.I),
                ),
            ):
                raise AutoAuthError("Notion verification-code submit was not found")
            page.wait_for_timeout(3_000)
        finally:
            mailbox.close()

    def authorize(self, authorization_url: str) -> None:
        state_path = None if self.force_notion_login else self.notion_state
        context = self._new_context(state_path)
        page = context.new_page()
        try:
            page.goto(
                authorization_url,
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            page.wait_for_timeout(2_000)
            if urlparse(page.url).path == "/login" or _is_visible(
                page.locator("input[type=email]"), timeout=1_000
            ):
                self._log_in_to_notion(page)

            checkbox = page.locator("input[type=checkbox]").first
            checkbox.wait_for(state="visible", timeout=30_000)
            if not checkbox.is_checked():
                checkbox.check()
            continue_control = page.get_by_role(
                "button", name=re.compile(r"^(Continue|继续)$")
            ).last
            continue_control.wait_for(state="visible", timeout=10_000)
            continue_control.click()
            page.wait_for_url(
                lambda url: urlparse(str(url)).hostname
                in {"localhost", "127.0.0.1"},
                timeout=30_000,
            )
            _save_state(context, self.notion_state)
        finally:
            context.close()


def _client_command(repo_root: Path, server_url: str, callback_port: int) -> list[str]:
    configured_spec = _configured_mcp_remote_spec(repo_root)
    client_js = repo_root / "node_modules" / "mcp-remote" / "dist" / "client.js"
    installed_package = repo_root / "node_modules" / "mcp-remote" / "package.json"
    installed_version = None
    if installed_package.is_file():
        installed_version = json.loads(installed_package.read_text(encoding="utf-8")).get(
            "version"
        )
    configured_version = (
        configured_spec.rsplit("@", 1)[1] if "@" in configured_spec else None
    )
    if client_js.is_file() and (
        configured_version is None or configured_version == installed_version
    ):
        return [process_executable("node"), str(client_js), server_url, str(callback_port)]
    client_bin = shutil.which("mcp-remote-client")
    if client_bin and configured_version is None:
        return [client_bin, server_url, str(callback_port)]
    return [
        process_executable("npx"),
        "--yes",
        f"--package={configured_spec}",
        "--",
        "mcp-remote-client",
        server_url,
        str(callback_port),
    ]


def _configured_mcp_remote_spec(repo_root: Path) -> str:
    config_path = repo_root / "configs" / "mcp_servers" / "notion_official.yaml"
    if config_path.is_file():
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        for argument in config.get("params", {}).get("args", []):
            if argument == "mcp-remote" or str(argument).startswith("mcp-remote@"):
                return str(argument)

    package_lock = repo_root / "package-lock.json"
    if package_lock.is_file():
        lock_data = json.loads(package_lock.read_text(encoding="utf-8"))
        version = (
            lock_data.get("packages", {})
            .get("node_modules/mcp-remote", {})
            .get("version")
        )
        if version:
            return f"mcp-remote@{version}"
    raise AutoAuthError("The configured mcp-remote version could not be determined")


def process_executable(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise AutoAuthError(f"Required executable is missing: {name}")
    return path


def _stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.send_signal(signal.SIGINT)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def ensure_auth(
    authorizer: BrowserAuthorizer,
    auth_dir: Path,
    callback_port: int,
    timeout_seconds: float,
    require_reauthorized: bool = False,
) -> str:
    env = os.environ.copy()
    env["MCP_REMOTE_CONFIG_DIR"] = str(auth_dir)
    env.setdefault("BROWSER", "/bin/true")
    command = _client_command(REPO_ROOT, DEFAULT_SERVER_URL, callback_port)
    process = subprocess.Popen(
        command,
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert process.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout_seconds
    output_tail = ""
    reauthorized = False
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                remainder = process.stdout.read()
                output_tail = (output_tail + remainder)[-16_384:]
                break
            if not selector.select(timeout=0.5):
                continue
            line = process.stdout.readline()
            if not line:
                continue
            output_tail = (output_tail + line)[-16_384:]
            if "Connected successfully!" in line:
                if require_reauthorized and not reauthorized:
                    raise AutoAuthError(
                        "mcp-remote connected without an OAuth round trip even "
                        "though --force-round-trip was requested; the stored "
                        "tokens were not cleared. Check that the auth directory "
                        "is the one mcp-remote reads (MCP_REMOTE_CONFIG_DIR + "
                        "mcp-remote version)."
                    )
                if not reauthorized and authorizer.force_notion_login:
                    print(
                        "note: --force-notion-login had no effect; mcp-remote "
                        "reused a still-valid token without prompting for a "
                        "login. Remove the stored tokens to force the round trip."
                    )
                return "reauthorized" if reauthorized else "valid"
            authorization_url = extract_authorization_url(output_tail)
            if authorization_url and not reauthorized:
                authorizer.authorize(authorization_url)
                reauthorized = True
                output_tail = ""
        raise AutoAuthError(
            "mcp-remote did not establish an authenticated connection; "
            + redact(output_tail[-2_000:]).strip()
        )
    finally:
        selector.close()
        _stop_process(process)


def _acquire_lock(path: Path, timeout_seconds: float):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    # The lock only serializes refreshes; its mode is not security-relevant,
    # and root-owned lock files left behind by a container run cannot be
    # chmodded from a host-side process.
    _harden(path)
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except BlockingIOError:
            if time.monotonic() >= deadline:
                handle.close()
                raise AutoAuthError(f"OAuth lock remained busy for {timeout_seconds:.0f}s")
            time.sleep(0.5)


def _harden_auth_files(auth_dir: Path) -> None:
    for pattern in ("*_tokens.json", "*_client_info.json", "*_code_verifier*"):
        for path in auth_dir.rglob(pattern):
            if path.is_file():
                _harden(path)
    for name in ("notion.env", "outlook_state.json", "notion_login_state.json"):
        path = auth_dir / name
        if path.is_file():
            _harden(path)


def _token_hash(server_url: str) -> str:
    """md5 hex digest of the server URL, mirroring mcp-remote's file naming."""
    return hashlib.md5(server_url.encode("utf-8")).hexdigest()


def _clear_stale_credentials(auth_dir: Path, server_url: str) -> None:
    """Delete the token set so mcp-remote is forced into a fresh OAuth round.

    Only the access/refresh tokens and the PKCE verifier are removed; the
    dynamically registered ``client_info.json`` is kept so Notion reuses the
    same client_id instead of minting a new one on every call.

    The auth directory is versioned per mcp-remote release
    (``mcp-remote-<version>/``); versioned subdirectories are scanned rather
    than a bare filesystem glob because OpenSSL/libc versions and
    ``**``-recursive globbing behave differently across hosts.
    """
    digest = _token_hash(server_url)
    removed: list[Path] = []
    for child in auth_dir.iterdir():
        if not child.is_dir():
            continue
        for suffix in ("_tokens.json", "_code_verifier.txt", "_code_verifier"):
            candidate = child / f"{digest}{suffix}"
            if candidate.exists():
                candidate.unlink()
                removed.append(candidate)
    if removed:
        for path in removed:
            print(f"notion_oauth=cleared: {path.name}")


def _launch_chromium(playwright):
    launch_args = ["--no-sandbox"] if os.geteuid() == 0 else []
    configured_path = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
    if configured_path:
        return playwright.chromium.launch(
            headless=True,
            args=launch_args,
            executable_path=configured_path,
        )

    expected_path = Path(playwright.chromium.executable_path)
    if expected_path.is_file():
        return playwright.chromium.launch(headless=True, args=launch_args)

    cache_root = Path.home() / ".cache" / "ms-playwright"
    candidates = sorted(cache_root.glob("chromium-*/chrome-linux*/chrome"))
    if not candidates:
        return playwright.chromium.launch(headless=True, args=launch_args)
    return playwright.chromium.launch(
        headless=True,
        args=launch_args,
        executable_path=str(candidates[0]),
    )


def _mailbox_from_env(env_file: Path) -> str | None:
    """Read MAILBOX_PROVIDER from the credential file, ignoring unknown values."""
    if not env_file.is_file():
        return None
    raw = (dotenv_values(env_file).get("MAILBOX_PROVIDER") or "").strip().lower()
    if raw in (MAILBOX_OUTLOOK, MAILBOX_NETEASE_163):
        return raw
    return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--auth-dir", type=Path, default=DEFAULT_AUTH_DIR)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--callback-port", type=int, default=DEFAULT_CALLBACK_PORT)
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--lock-held", action="store_true")
    parser.add_argument("--verify-refresh", action="store_true", help="Verify refresh capability before preparation; transient failures retain authorization and abort")
    parser.add_argument(
        "--mailbox",
        choices=(MAILBOX_OUTLOOK, MAILBOX_NETEASE_163),
        default=None,
        help="Mailbox holding the Notion code; defaults to MAILBOX_PROVIDER in "
        "the credential file, then to outlook",
    )
    parser.add_argument(
        "--force-notion-login",
        action="store_true",
        help="When a login does happen, ignore the saved Notion browser session "
        "and authenticate from scratch. This does NOT force a login: whether "
        "one happens at all is mcp-remote's decision, and it will reuse a valid "
        "token without ever reaching this path. Delete "
        "mcp-remote-*/<hash>_tokens.json and notion_login_state.json to force "
        "the full round trip.",
    )
    parser.add_argument(
        "--force-round-trip",
        action="store_true",
        help="Delete the stored tokens before starting, forcing mcp-remote to "
        "authorize from scratch instead of reusing a still-valid access token. "
        "This is the deterministic entry point for a cold-start deploy: without "
        "it, mcp-remote silently skips the OAuth round when the access token is "
        "still live even though the refresh token is dead ('Connected "
        "successfully!' with no authorization URL). With this flag, the run "
        "MUST end 'reauthorized'; 'valid' means files were not actually "
        "cleared and the recovery is rejected.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    auth_dir = args.auth_dir.resolve()
    auth_dir.mkdir(parents=True, exist_ok=True)
    env_file = (args.env_file or auth_dir / "notion.env").resolve()
    mailbox = args.mailbox or _mailbox_from_env(env_file) or DEFAULT_MAILBOX
    lock_handle = None
    try:
        if not args.lock_held:
            lock_handle = _acquire_lock(
                auth_dir / "notion_official_refresh.lock",
                DEFAULT_LOCK_WAIT_SECONDS,
            )
        if args.verify_refresh:
            from utils.app_specific.notion.notion_token_refresh import refresh_saved_authorization
        require_reauthorized = args.force_round_trip
        if args.force_round_trip:
            _clear_stale_credentials(auth_dir, DEFAULT_SERVER_URL)
        elif args.verify_refresh:
            refresh_status = refresh_saved_authorization(auth_dir)
            print(f"notion_refresh={refresh_status}")
            if refresh_status == 'rejected':
                require_reauthorized = True
                _clear_stale_credentials(auth_dir, DEFAULT_SERVER_URL)
        with sync_playwright() as playwright:
            browser = _launch_chromium(playwright)
            try:
                authorizer = BrowserAuthorizer(
                    browser,
                    auth_dir,
                    env_file,
                    force_notion_login=args.force_notion_login,
                    mailbox=mailbox,
                )
                status = ensure_auth(
                    authorizer,
                    auth_dir,
                    args.callback_port,
                    args.timeout_seconds,
                    require_reauthorized=require_reauthorized,
                )
                if args.verify_refresh and status == 'reauthorized':
                    if refresh_saved_authorization(auth_dir) != 'refreshed':
                        raise AutoAuthError('New Notion authorization cannot be refreshed; preparation stopped')
                    # Check the replacement access token, not only the login response.
                    if ensure_auth(authorizer, auth_dir, args.callback_port, args.timeout_seconds) != 'valid':
                        raise AutoAuthError('Refreshed Notion authorization still requires login; preparation stopped')
            finally:
                browser.close()
        _harden_auth_files(auth_dir)
        print(f"notion_oauth={status}")
        return 0
    except ManualRecoveryRequired as exc:
        print(f"notion_oauth=manual_microsoft_recovery_required: {redact(str(exc))}")
        return 20
    except Exception as exc:
        print(f"notion_oauth=failed: {redact(str(exc))}")
        return 1
    finally:
        if lock_handle is not None:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            lock_handle.close()


if __name__ == "__main__":
    sys.exit(main())
