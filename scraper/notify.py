"""
Apprise notification helper.

Configured entirely through environment variables so it stays inert in
local development and CI unless you opt in:

  APPRISE_URLS                whitespace- or comma-separated list of Apprise
                              service URLs. e.g.
                                "mailto://user:pass@smtp.gmail.com"
                                "tgram://bottoken/chatid discord://wh/id"
                              See https://github.com/caronc/apprise/wiki
                              for the supported services and URL formats.

  APPRISE_NOTIFY_ON_SUCCESS   any truthy value → also send a notification
                              after a clean run (default: only notify when
                              there were warnings or a fatal error).

Designed to never break the scraper: missing apprise package, malformed
URLs, network failures all degrade to a printed warning.
"""

import os
import re


def _targets() -> list[str]:
    raw = os.environ.get("APPRISE_URLS", "").strip()
    if not raw:
        return []
    return [t for t in re.split(r"[\s,]+", raw) if t]


def _on_success_enabled() -> bool:
    return (os.environ.get("APPRISE_NOTIFY_ON_SUCCESS") or "").strip().lower() \
        in ("1", "true", "yes", "on")


def notify(*, title: str, body: str, warning: bool) -> None:
    """Send `body` to every configured Apprise URL.

    No-op if APPRISE_URLS is empty, or if warning=False and
    APPRISE_NOTIFY_ON_SUCCESS isn't set. Never raises.
    """
    urls = _targets()
    if not urls:
        return
    if not warning and not _on_success_enabled():
        return

    try:
        import apprise
    except ImportError:
        print("  ⚠ apprise not installed; skipping notification", flush=True)
        return

    try:
        a = apprise.Apprise()
        for u in urls:
            a.add(u)
        notify_type = (
            apprise.NotifyType.WARNING if warning else apprise.NotifyType.SUCCESS
        )
        ok = a.notify(title=title, body=body, notify_type=notify_type)
        if not ok:
            print("  ⚠ apprise notification returned failure", flush=True)
    except BaseException as e:
        # Apprise loads optional dependencies lazily; some plugins use
        # native modules that raise pyo3_runtime.PanicException (a
        # BaseException subclass, not Exception) when their bindings
        # are broken. Catching Exception isn't enough — and a failed
        # notification must never take the scraper down.
        print(f"  ⚠ apprise notification raised: {e}", flush=True)
