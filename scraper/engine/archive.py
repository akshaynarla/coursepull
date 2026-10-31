"""Copies of dead pages and files from the Internet Archive's Wayback Machine.

Course reading lists rot: blogs move and sites disappear (blog.stockapp.com no longer
resolves). The Wayback Machine's availability API is public and keyless; only the
dead link itself is sent.
"""

from __future__ import annotations

import ipaddress
import logging
from urllib.parse import urlparse

import requests

from ..config import REQUEST_TIMEOUT
from .http import session

log = logging.getLogger(__name__)

_AVAILABLE = "https://archive.org/wayback/available"


def _public(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    if not host or host == "localhost" or host.endswith((".local", ".localhost")):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True  # a domain name
    return not (address.is_private or address.is_loopback or address.is_link_local)


def archived_timestamp(url: str) -> str | None:
    """Timestamp (YYYYMMDDhhmmss) of the closest good snapshot of url, if the Archive has one."""
    if not _public(url):  # the Archive cannot have local or private addresses: don't ask
        return None
    try:
        resp = session().get(_AVAILABLE, params={"url": url}, timeout=REQUEST_TIMEOUT)
        snapshot = ((resp.json() or {}).get("archived_snapshots") or {}).get("closest") or {}
    except (requests.RequestException, ValueError) as exc:
        log.info("Wayback lookup failed for %s: %s", url, exc)
        return None
    if not snapshot.get("available") or not str(snapshot.get("status", "200")).startswith("2"):
        return None
    return snapshot.get("timestamp")


def page_url(timestamp: str, url: str) -> str:
    """The archived page without the Wayback toolbar, links rewritten to archived copies."""
    return f"https://web.archive.org/web/{timestamp}if_/{url}"


def raw_url(timestamp: str, url: str) -> str:
    """The archived file exactly as it was served."""
    return f"https://web.archive.org/web/{timestamp}id_/{url}"


def note(timestamp: str) -> str:
    return f"the original is gone; saved the Internet Archive's copy from {timestamp[:4]}-{timestamp[4:6]}-{timestamp[6:8]}"
