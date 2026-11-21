"""URL helpers for the Portal_Origin. Pure Python.

``callback_urls``, ``logout_urls`` and ``cors_origins`` build their results with plain string
concatenation on the portal origin, so a CDK token string works as the origin. Only the
local development URLs (plain strings from Stage_Config) are parsed.
"""

from collections.abc import Iterable, Sequence
from urllib.parse import urlsplit


def is_https_origin(url: str) -> bool:
    """Return True for ``https://host[:port]`` with no userinfo, path, query or fragment."""
    if not url or url != url.strip() or any(ch.isspace() for ch in url):
        return False
    # urlsplit drops an empty query or fragment ("https://a.com?"), so reject the markers.
    if "?" in url or "#" in url:
        return False
    try:
        parts = urlsplit(url)
        _ = parts.port  # raises ValueError for an invalid port
    except ValueError:
        return False
    return (
        parts.scheme == "https"
        and bool(parts.hostname)
        and parts.username is None
        and parts.password is None
        and parts.path == ""
        and not parts.netloc.endswith(":")
    )


def origin_of(url: str) -> str:
    """Return ``scheme://host[:port]`` of ``url``, dropping userinfo, path, query and fragment.

    Parses ``url``, so pass plain URLs only (for example local development URLs), not tokens.
    """
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        raise ValueError(f"Not an absolute URL: {url!r}")
    host_and_port = parts.netloc.rpartition("@")[2]
    return f"{parts.scheme}://{host_and_port}"


def _unique(items: Iterable[str]) -> list[str]:
    """Deduplicate while keeping first-seen order, and refuse wildcards."""
    result = list(dict.fromkeys(items))
    if any("*" in item for item in result):
        raise ValueError("Wildcard '*' is not allowed in URLs or origins")
    return result


def callback_urls(origin: str, path: str, local_dev_urls: Sequence[str]) -> list[str]:
    """Return ``origin + path`` followed by the local development URLs, deduplicated."""
    return _unique([origin + path, *local_dev_urls])


def logout_urls(origin: str, path: str, local_dev_urls: Sequence[str]) -> list[str]:
    """Return ``origin + path`` and ``origin_of(url) + path`` for each local URL, deduplicated."""
    return _unique([origin + path, *(origin_of(url) + path for url in local_dev_urls)])


def cors_origins(origin: str, local_dev_urls: Sequence[str]) -> list[str]:
    """Return the portal origin and the origin of each local development URL, deduplicated."""
    return _unique([origin, *(origin_of(url) for url in local_dev_urls)])
