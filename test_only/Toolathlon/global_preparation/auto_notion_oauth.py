"""
Automated Notion official MCP OAuth setup
=========================================

Replaces the manual step in ``special_setup_notion_official``: instead of the
operator copying an authorization URL into a browser, clicking through Notion's
consent screen and ticking pages by hand, a Playwright browser carrying a
**pre-saved Notion login session** does it.

What this does NOT do
---------------------
It does not log in to Notion. The login session comes from
``configs/notion_state.json``, produced once by hand via::

    uv run python -m utils.app_specific.notion.notion_login_helper \\
        --state_path ./configs/notion_state.json

If that state has expired (Notion bounces us to the login page), this script
**exits non-zero with a clear message** rather than falling back to an
interactive browser. Refresh the state file and re-run.

How the pieces fit
------------------
``special_setup_notion_official`` boots the ``notion_official`` MCP server,
which is ``npx mcp-remote https://mcp.notion.com/mcp``. With no cached tokens,
mcp-remote runs the MCP OAuth dance (RFC 9728 discovery -> RFC 7591 dynamic
client registration -> PKCE -> localhost redirect). It prints the authorization
URL via ``console.error`` and tries to ``open()`` it.

We run that script as a child process, scrape the URL off its merged output, and
drive the consent screen with Playwright. mcp-remote's own callback server on
port 9553 receives the code and writes the tokens itself -- we never perform the
token exchange, so the on-disk format stays exactly what the eval containers
expect.

Usage
-----
    uv run -m global_preparation.auto_notion_oauth            # real run
    uv run -m global_preparation.auto_notion_oauth --dry-run  # stop before clicking Authorize
    uv run -m global_preparation.auto_notion_oauth --headed   # watch it happen

``--dry-run`` drives all the way to the consent screen, screenshots it, and
exits without clicking anything -- the way to verify selectors and check whether
the saved login state is still alive, without consuming a single-use
authorization code.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path

# --------------------------------------------------------------------------
# Constants derived from the mcp-remote 0.1.16 actually installed in this repo
# (node_modules/mcp-remote). Verified:
#   md5("https://mcp.notion.com/mcp") == cb42d1a06ae8db4e5585a26f2e5ca947
#   calculateDefaultPort() = 3335 + (hash[:4] as hex) % 45816 == 9553
# and the on-disk client_info.json from the previous manual run records
# redirect_uri http://localhost:9553/oauth/callback, confirming both.
# --------------------------------------------------------------------------
NOTION_MCP_URL = "https://mcp.notion.com/mcp"
SERVER_URL_HASH = "cb42d1a06ae8db4e5585a26f2e5ca947"
MCP_REMOTE_VERSION = "0.1.16"
CALLBACK_PORT = 9553

REPO_ROOT = Path(__file__).resolve().parent.parent
AUTH_DIR = REPO_ROOT / "configs" / ".mcp-auth" / f"mcp-remote-{MCP_REMOTE_VERSION}"
TOKENS_PATH = AUTH_DIR / f"{SERVER_URL_HASH}_tokens.json"
LOCK_PATH = AUTH_DIR / f"{SERVER_URL_HASH}_lock.json"
STATE_PATH = REPO_ROOT / "configs" / "notion_state.json"

SETUP_MODULE = "global_preparation.special_setup_notion_official"

# mcp-remote prints "Please authorize this client by visiting:\n<url>".
# Anchored on the /authorize path so we don't grab discovery metadata URLs,
# which are also on mcp.notion.com.
AUTHORIZE_URL_RE = re.compile(
    r"https://mcp\.notion\.com/(?:authorize|oauth/authorize)\?[^\s\"'>]+"
)

# Notion's OAuth consent screen. Best-effort: Notion ships UI changes without
# notice, which is why --dry-run exists and why every failure path dumps a
# screenshot plus the page HTML for selector repair.
CONSENT_MARKERS = ("select pages", "choose pages", "authorize", "grant access")
AUTHORIZE_BUTTON_TEXTS = (
    "Allow access",
    "Authorize",
    "Grant access",
    "Allow",
    "Agree",
    "Continue",
)


class AutomationError(RuntimeError):
    """A condition the operator has to resolve by hand."""


def log(msg: str) -> None:
    print(f"[auto-notion-oauth] {msg}", flush=True)


# --------------------------------------------------------------------------
# Preflight
# --------------------------------------------------------------------------
def preflight(dry_run: bool) -> None:
    """Fail before spawning anything, on the conditions we can predict."""
    if not STATE_PATH.exists():
        raise AutomationError(
            f"Notion login state not found at {STATE_PATH}\n"
            f"\n"
            f"  Produce it once, by hand, from {REPO_ROOT}:\n"
            f"    uv run python -m utils.app_specific.notion.notion_login_helper "
            f"--state_path ./configs/notion_state.json\n"
            f"\n"
            f"  That command opens a real browser window; log in, then press "
            f"ENTER in the terminal. Afterwards this script runs unattended "
            f"until Notion expires the session."
        )

    if not dry_run and TOKENS_PATH.exists():
        log(
            "NOTE: tokens already exist on disk. mcp-remote will try to refresh "
            "them instead of starting a fresh auth flow, so there may be no "
            "authorization URL and no consent screen to click. That is the "
            "normal steady state."
        )


def describe_state_age() -> str:
    """Best-effort hint about how stale the saved login is."""
    try:
        cookies = json.loads(STATE_PATH.read_text()).get("cookies", [])
        if not cookies:
            return "no cookies in state file"
        expiries = [c["expires"] for c in cookies if c.get("expires", -1) > 0]
        if not expiries:
            return f"{len(cookies)} cookies, none with explicit expiry"
        remaining = min(expiries) - time.time()
        if remaining <= 0:
            return f"{len(cookies)} cookies, at least one ALREADY EXPIRED"
        return f"{len(cookies)} cookies, soonest expires in {remaining / 86400:.1f} days"
    except Exception as exc:
        return f"(could not inspect state file: {exc})"


def tokens_are_valid() -> bool:
    if not TOKENS_PATH.exists():
        return False
    try:
        data = json.loads(TOKENS_PATH.read_text())
    except Exception:
        return False
    if not data.get("access_token"):
        return False
    expires_at = data.get("expires_at")
    if isinstance(expires_at, (int, float)):
        return expires_at > time.time() * 1000
    return True


def derive_page_names() -> list[str]:
    """Pull the two page titles straight out of the eval's own token config.

    "https://app.notion.com/p/Notion-Source-Page-310487725f53829cb2fd81c1b63df6d3"
      -> "Notion Source Page"
    """
    try:
        sys.path.insert(0, str(REPO_ROOT))
        from configs.token_key_session import all_token_key_session as tks

        names = []
        for key in ("source_notion_page_url", "eval_notion_page_url"):
            slug = (tks.get(key) or "").rstrip("/").split("/")[-1].split("?")[0]
            slug = re.sub(r"-[0-9a-f]{32}$", "", slug)
            slug = re.sub(r"-[0-9a-f]{8,}$", "", slug)
            if slug:
                names.append(slug.replace("-", " ").strip())
        if names:
            log(f"Page names from token_key_session: {names}")
        return names
    except Exception as exc:
        log(
            f"Could not derive page names from token_key_session ({exc}); pass "
            f"--page explicitly if the resulting grant turns out too narrow."
        )
        return []


# --------------------------------------------------------------------------
# Driving the child process
# --------------------------------------------------------------------------
async def spawn_setup_and_capture_url(timeout: float) -> tuple[asyncio.subprocess.Process, str]:
    """Run the setup script until mcp-remote prints its authorize URL.

    Returns ``(process, url)``. The process is still alive on success -- on a
    successful auth it sits in ``await asyncio.sleep(inf)`` holding the MCP
    session open, so waiting for exit would hang forever. The caller owns
    cleanup.

    Note the stream choice: the setup script's *stdout* carries the JSON-RPC
    frames, so all of mcp-remote's human-readable chatter -- the URL included --
    goes to *stderr*. We merge stderr into stdout and read incrementally.
    """
    log(f"Spawning: uv run -m {SETUP_MODULE}")

    if LOCK_PATH.exists():
        log(
            f"NOTE: a lockfile is present at {LOCK_PATH.name}. mcp-remote uses "
            f"it to hand off to another live instance; if this run appears to "
            f"hang, that is why -- delete the file and retry."
        )

    proc = await asyncio.create_subprocess_exec(
        "uv", "run", "-m", SETUP_MODULE,
        cwd=str(REPO_ROOT),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    deadline = time.monotonic() + timeout
    buffer = ""

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AutomationError(
                f"Timed out after {timeout:.0f}s waiting for an authorization "
                f"URL.\n"
                f"\n"
                f"  If tokens already exist for this account, no auth is needed "
                f"and there is nothing to click -- that is success, not "
                f"failure.\n"
                f"\n"
                f"--- output so far ---\n{buffer}"
            )

        try:
            chunk = await asyncio.wait_for(proc.stdout.read(4096), timeout=min(remaining, 5.0))
        except asyncio.TimeoutError:
            if proc.returncode is not None:
                raise AutomationError(
                    f"Setup script exited with code {proc.returncode} before "
                    f"printing an authorization URL.\n--- output ---\n{buffer}"
                )
            continue

        if not chunk:
            # EOF: child died or closed the pipe.
            if proc.returncode is None:
                try:
                    await asyncio.wait_for(proc.wait(), timeout=10)
                except asyncio.TimeoutError:
                    pass
            raise AutomationError(
                f"Setup script closed its output (exit code {proc.returncode}) "
                f"before printing an authorization URL.\n--- output ---\n{buffer}"
            )

        text = chunk.decode("utf-8", errors="replace")
        buffer += text
        sys.stdout.write(text)
        sys.stdout.flush()

        match = AUTHORIZE_URL_RE.search(buffer)
        if match:
            log(f"Captured authorization URL ({len(match.group(0))} chars)")
            return proc, match.group(0)

        if proc.returncode is not None:
            raise AutomationError(
                f"Setup script exited with code {proc.returncode} before "
                f"printing an authorization URL.\n--- output ---\n{buffer}"
            )


async def shutdown(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    log("Shutting down the setup script.")
    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=10)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()


# --------------------------------------------------------------------------
# Playwright: the part that used to be your mouse
# --------------------------------------------------------------------------
def drive_consent(
    authorize_url: str,
    *,
    page_names: list[str],
    headless: bool,
    dry_run: bool,
    navigate_timeout_ms: int,
    consent_timeout_ms: int,
    artifact_dir: Path,
) -> None:
    """Open the authorize URL with the saved login state and click through.

    Raises AutomationError when the login state is dead or the consent screen
    can't be recognised.
    """
    from playwright.sync_api import sync_playwright
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    artifact_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        # The engine must match the one notion_login_helper used to create
        # STATE_PATH -- storage_state is not reliably portable across engines.
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(
            storage_state=str(STATE_PATH),
            viewport={"width": 1440, "height": 900},
        )
        page = context.new_page()

        def dump(tag: str) -> None:
            """Save a screenshot + HTML so a human can repair selectors."""
            shot = artifact_dir / f"{tag}.png"
            html = artifact_dir / f"{tag}.html"
            try:
                page.screenshot(path=str(shot), full_page=True)
                html.write_text(page.content(), encoding="utf-8")
                log(f"  wrote {shot.name} and {html.name}")
            except Exception as exc:
                log(f"  (could not write artifacts: {exc})")

        log("Opening the authorization URL in Playwright...")
        page.goto(authorize_url, wait_until="domcontentloaded", timeout=navigate_timeout_ms)

        # --- Detect a dead login session ------------------------------------
        # Notion bounces an unauthenticated visitor to its login page. This
        # script's contract is "fail loudly, never prompt", so it is a hard
        # error rather than a fallback.
        if _looks_like_login(page.url, page.title()):
            dump("01-login-required")
            raise AutomationError(
                f"Notion redirected to a login page ({page.url})\n"
                f"\n"
                f"  The saved login state at {STATE_PATH} has expired or was "
                f"invalidated -- password change, manual sign-out, or session "
                f"timeout.\n"
                f"\n"
                f"  Refresh it by hand (the only manual step left):\n"
                f"    cd {REPO_ROOT}\n"
                f"    uv run python -m utils.app_specific.notion.notion_login_helper "
                f"--state_path ./configs/notion_state.json\n"
                f"\n"
                f"  Then re-run this script."
            )

        # --- Wait for the consent screen -------------------------------------
        log("Waiting for the consent screen...")
        try:
            page.wait_for_load_state("networkidle", timeout=30_000)
        except PlaywrightTimeoutError:
            pass

        if not _find_consent_screen(page, consent_timeout_ms):
            dump("02-no-consent-screen")
            raise AutomationError(
                f"Reached {page.url} but could not recognise a consent screen.\n"
                f"  Screenshot + HTML dumped to {artifact_dir} for selector "
                f"repair -- Notion may have redesigned this page."
            )

        log("Consent screen detected.")
        dump("03-consent-screen")

        # --- Tick the pages --------------------------------------------------
        for name in page_names:
            if _select_page_by_name(page, name):
                log(f"  ticked: {name}")
            else:
                log(
                    f"  WARNING: no row matching {name!r}. Continuing -- if the "
                    f"grant ends up too narrow the duplicator will fail later "
                    f"with a permissions error."
                )

        dump("04-after-selection")

        if dry_run:
            log("--dry-run: stopping before clicking the authorize button.")
            log(f"  Inspect {artifact_dir / '04-after-selection.png'} to confirm "
                f"the right pages are ticked.")
            context.close()
            browser.close()
            return

        # --- Click authorize --------------------------------------------------
        if not _click_authorize(page):
            dump("05-no-authorize-button")
            raise AutomationError(
                f"Consent screen shown but no authorize button matched. Tried: "
                f"{', '.join(AUTHORIZE_BUTTON_TEXTS)}\n"
                f"  See {artifact_dir} for the dumped page."
            )

        log("Clicked authorize. Waiting for the localhost callback...")

        # mcp-remote's callback server answers with a small HTML page that calls
        # window.close(). The window may or may not actually close, so don't
        # block on it -- token verification downstream is the real signal.
        try:
            page.wait_for_load_state("networkidle", timeout=30_000)
        except PlaywrightTimeoutError:
            pass
        dump("06-post-authorize")

        context.close()
        browser.close()


def _looks_like_login(url: str, title: str) -> bool:
    u = url.lower()
    t = (title or "").lower()
    if "/login" in u or "/signup" in u:
        return True
    return any(marker in t for marker in ("log in", "sign in", "sign up"))


def _find_consent_screen(page, timeout_ms: int) -> bool:
    """Poll the page text for any telltale consent-screen string."""
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        try:
            body = (page.inner_text("body") or "").lower()
        except Exception:
            body = ""
        if any(marker in body for marker in CONSENT_MARKERS):
            return True
        page.wait_for_timeout(500)
    return False


def _select_page_by_name(page, name: str) -> bool:
    """Tick the checkbox whose row carries ``name``.

    Three strategies, most semantic first: the accessible checkbox role, then
    climbing from the text node to the nearest ancestor holding a checkbox
    input, then an ancestor with ``role="checkbox"``.
    """
    pattern = re.compile(re.escape(name), re.I)

    try:
        box = page.get_by_role("checkbox", name=pattern)
        if box.count() > 0:
            box.first.check(timeout=5_000)
            return True
    except Exception:
        pass

    try:
        node = page.get_by_text(pattern).first
        node.wait_for(state="visible", timeout=5_000)

        row = node.locator("xpath=ancestor::*[.//input[@type='checkbox']][1]")
        if row.count() > 0:
            row.locator("input[type='checkbox']").first.check(timeout=5_000)
            return True

        row = node.locator("xpath=ancestor::*[@role='checkbox'][1]")
        if row.count() > 0:
            row.first.click(timeout=5_000)
            return True
    except Exception:
        pass

    return False


def _click_authorize(page) -> bool:
    for text in AUTHORIZE_BUTTON_TEXTS:
        try:
            btn = page.get_by_role("button", name=re.compile(rf"^{re.escape(text)}", re.I))
            if btn.count() > 0 and btn.first.is_enabled():
                btn.first.click(timeout=10_000)
                return True
        except Exception:
            continue
    return False


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
async def run(args: argparse.Namespace) -> int:
    preflight(dry_run=args.dry_run)

    if args.dry_run:
        log("DRY RUN: no authorization code will be consumed.")
    log(f"Login state : {STATE_PATH}")
    log(f"              {describe_state_age()}")
    log(f"Tokens land : {TOKENS_PATH}")

    proc, url = await spawn_setup_and_capture_url(timeout=args.url_timeout)

    try:
        # mcp-remote has registered its callback server by now; give it a beat
        # to settle before pointing a browser at the authorize URL.
        await asyncio.sleep(1.0)

        page_names = args.page or derive_page_names()

        # Playwright's sync API blocks, so run it off the event loop; the child
        # process keeps being drained in the meantime.
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: drive_consent(
                url,
                page_names=page_names,
                headless=not args.headed,
                dry_run=args.dry_run,
                navigate_timeout_ms=args.navigate_timeout * 1000,
                consent_timeout_ms=args.consent_timeout * 1000,
                artifact_dir=Path(args.artifact_dir),
            ),
        )

        if args.dry_run:
            log("Dry run complete -- nothing was authorized.")
            return 0

        # --- Verify the end state --------------------------------------------
        log("Waiting for mcp-remote to write tokens...")
        deadline = time.monotonic() + args.token_timeout
        while time.monotonic() < deadline:
            if tokens_are_valid():
                log(f"SUCCESS: {TOKENS_PATH.name} written and unexpired.")
                log(
                    "notion_official is ready. Containers pick this up from "
                    "configs/.mcp-auth via bind-mount."
                )
                return 0
            await asyncio.sleep(1.0)

        raise AutomationError(
            f"Authorize was clicked but {TOKENS_PATH} did not appear within "
            f"{args.token_timeout:.0f}s.\n"
            f"  The code exchange likely failed -- check the setup script's "
            f"output above for an OAuth error."
        )

    finally:
        await shutdown(proc)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Unattended Notion official MCP OAuth setup.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Drive to the consent screen, screenshot it, stop before clicking authorize.",
    )
    parser.add_argument(
        "--headed", action="store_true",
        help="Show the browser window (useful when repairing selectors).",
    )
    parser.add_argument(
        "--page", action="append", metavar="NAME",
        help="Page title to tick on the consent screen. Repeatable. Defaults to "
             "the two pages named in configs/token_key_session.py.",
    )
    parser.add_argument("--url-timeout", type=float, default=120.0,
                        help="Seconds to wait for mcp-remote to print the authorize URL.")
    parser.add_argument("--consent-timeout", type=float, default=60.0,
                        help="Seconds to wait for the consent screen to appear.")
    parser.add_argument("--navigate-timeout", type=float, default=60.0,
                        help="Seconds for the initial page load.")
    parser.add_argument("--token-timeout", type=float, default=60.0,
                        help="Seconds to wait for tokens.json after authorizing.")
    parser.add_argument("--artifact-dir", default="./configs/notion_oauth_debug",
                        help="Where to dump screenshots/HTML on failure and in dry-run.")
    args = parser.parse_args()

    try:
        return asyncio.run(run(args))
    except AutomationError as exc:
        print(f"\n[auto-notion-oauth] FAILED\n\n{exc}\n", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n[auto-notion-oauth] interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        import traceback

        print(f"\n[auto-notion-oauth] UNEXPECTED ERROR: {exc}\n", file=sys.stderr)
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    sys.exit(main())
