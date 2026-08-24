from __future__ import annotations

import re
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

SUPPORTED_HOST_MARKERS = (
    "ficbook.net",
    "ficbook.com",
    "archiveofourown.org",
    "wattpad.com",
    "hogwartsnet.ru",
    "litnet.com",
    "ranobelib.me",
)


def extract_url(text: str) -> str | None:
    for chunk in text.split():
        candidate = chunk.strip("()[]<>,.!?\"'")
        if is_supported_story_url(candidate):
            return candidate
    return None


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    scheme = parts.scheme or "https"
    host = parts.netloc.lower()
    if "ficbook." in host:
        host = host.replace("www.", "").replace("ficbook.com", "ficbook.net")
    if "hogwartsnet.ru" in host:
        scheme = "http"
        host = "hogwartsnet.ru"
    if host.replace("www.", "") == "litnet.com":
        scheme = "https"
        host = "litnet.com"
    if host.replace("www.", "") == "ranobelib.me":
        scheme = "https"
        host = "ranobelib.me"
    path = parts.path.rstrip("/")
    query = parts.query
    if "readfic" in path:
        segments = [segment for segment in path.split("/") if segment]
        if len(segments) >= 2:
            path = f"/{segments[0]}/{segments[1]}"
            query = ""
    if "hogwartsnet.ru" in host:
        params = parse_qs(query)
        fid = params.get("fid", [""])[0]
        query = urlencode({"fid": fid}) if fid else query
    if host == "litnet.com":
        match = re.match(r"/([a-z]{2})/(?:book|reader)/([^/?]+-b\d+)", path, re.I)
        if match:
            path = f"/{match.group(1).lower()}/reader/{match.group(2)}"
            query = ""
    if host == "ranobelib.me":
        slug = next(
            (segment for segment in path.split("/") if re.fullmatch(r"\d+--[A-Za-z0-9_-]+", segment)),
            "",
        )
        if slug:
            path = f"/ru/{slug}"
            query = ""
    cleaned = urlunsplit((scheme, host, path, query, ""))
    return cleaned.split("#", 1)[0]


def is_supported_story_url(url: str) -> bool:
    value = url if re.match(r"^https?://", url, re.I) else f"https://{url}"
    parts = urlsplit(value)
    host = parts.netloc.lower().replace("www.", "")
    path = parts.path.lower()
    if host in {"ficbook.net", "ficbook.com"}:
        return "/readfic/" in path
    if host == "archiveofourown.org":
        return "/works/" in path
    if host == "wattpad.com":
        return path.startswith("/story/") or bool(re.match(r"/[0-9]+", path))
    if host == "hogwartsnet.ru":
        return "/mfanf/ffshowfic.php" in path and bool(parse_qs(parts.query).get("fid"))
    if host == "litnet.com":
        return bool(re.match(r"/[a-z]{2}/(?:book|reader)/[^/]+-b\d+", path))
    if host == "ranobelib.me":
        return any(re.fullmatch(r"\d+--[a-z0-9_-]+", segment) for segment in path.split("/"))
    return False


def site_key(url: str) -> str:
    host = urlsplit(url).netloc.lower().replace("www.", "")
    if host in {"ficbook.net", "ficbook.com"}:
        return "ficbook.net"
    if host == "archiveofourown.org":
        return "archiveofourown.org"
    if host == "wattpad.com":
        return "www.wattpad.com"
    if host == "hogwartsnet.ru":
        return "hogwartsnet.ru"
    if host == "litnet.com":
        return "litnet.com"
    if host == "ranobelib.me":
        return "ranobelib.me"
    return host


def is_ficbook_url(url: str) -> bool:
    return site_key(url) == "ficbook.net"


def is_hogwartsnet_url(url: str) -> bool:
    return site_key(url) == "hogwartsnet.ru"


def is_litnet_url(url: str) -> bool:
    return site_key(url) == "litnet.com"


def is_ranobelib_url(url: str) -> bool:
    return site_key(url) == "ranobelib.me"


def site_display_name(url: str) -> str:
    site = site_key(url)
    labels = {
        "ficbook.net": "Ficbook",
        "archiveofourown.org": "AO3",
        "www.wattpad.com": "Wattpad",
        "hogwartsnet.ru": "Hogwartsnet",
        "litnet.com": "Litnet",
        "ranobelib.me": "RanobeLIB",
    }
    return labels.get(site, site or "Сайт")
