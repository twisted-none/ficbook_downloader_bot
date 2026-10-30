from __future__ import annotations

import re
from urllib.parse import SplitResult, parse_qs, urlencode, urlsplit, urlunsplit

SUPPORTED_HOST_MARKERS = (
    "ficbook.net",
    "ficbook.com",
    "fikbooks.ru",
    "archiveofourown.org",
    "wattpad.com",
    "hogwartsnet.ru",
    "litnet.com",
    "ranobelib.me",
    "zahleb.me",
)

FICBOOK_ID_PATTERN = r'(?:[0-9]+|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})'
_COLLECTION_PATH = re.compile(rf'/collections/({FICBOOK_ID_PATTERN})(?:/(?:page|p)/[0-9]{{1,9}})?/?', re.I)
FIKBOOKS_HOSTS = frozenset({'fikbooks.ru', 'www.fikbooks.ru'})
FIKBOOKS_PATH = re.compile(r'/readfic/([1-9][0-9]{0,17})(?:/([1-9][0-9]{0,17}))?/?')
ZAHLEB_HOSTS = frozenset({'zahleb.me', 'www.zahleb.me'})
ZAHLEB_PATH = re.compile(r'/story/((?:[A-Za-z0-9][A-Za-z0-9_-]{0,240}-)?([A-Za-z0-9]{10}))'
                         r'(?:/([A-Za-z0-9]{10}))?/?')


def _story_parts(url: str) -> SplitResult:
    if (len(url) > 4096 or '\\' in url
            or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in url)):
        raise ValueError('Invalid story URL')
    parts = urlsplit(url)
    host = (parts.hostname or '').removeprefix('www.')
    if (parts.scheme not in {'http', 'https'} or host not in SUPPORTED_HOST_MARKERS
            or parts.username is not None or parts.password is not None
            or parts.port not in {None, 443 if parts.scheme == 'https' else 80}):
        raise ValueError('Invalid story URL')
    return parts


def normalize_zahleb_url(url: str) -> str:
    parts = _story_parts(url)
    match = ZAHLEB_PATH.fullmatch(parts.path)
    if parts.hostname not in ZAHLEB_HOSTS or match is None:
        raise ValueError('Invalid Zahleb URL')
    return 'https://zahleb.me/story/' + match.group(1)


def normalize_fikbooks_url(url: str) -> str:
    parts = _story_parts(url)
    match = FIKBOOKS_PATH.fullmatch(parts.path)
    if parts.hostname not in FIKBOOKS_HOSTS or match is None:
        raise ValueError('Invalid Fikbooks URL')
    return 'https://fikbooks.ru/readfic/' + match.group(1)


def normalize_collection_url(url: str) -> str:
    parts = _story_parts(url)
    match = _COLLECTION_PATH.fullmatch(parts.path)
    if (parts.hostname not in {'ficbook.net', 'www.ficbook.net', 'ficbook.com', 'www.ficbook.com'}
            or match is None):
        raise ValueError('Invalid collection URL')
    return f'https://ficbook.net/collections/{match.group(1).lower()}'


def is_ficbook_collection_url(url: str) -> bool:
    try:
        normalize_collection_url(url)
        return True
    except ValueError:
        return False


def extract_url(text: str) -> str | None:
    for chunk in text.split():
        candidate = chunk.strip("()[]<>,.!?\"'")
        if is_supported_story_url(candidate):
            return candidate
    return None


def normalize_url(url: str) -> str:
    parts = _story_parts(url)
    if parts.hostname in ZAHLEB_HOSTS:
        return normalize_zahleb_url(url)
    if parts.hostname in FIKBOOKS_HOSTS:
        return normalize_fikbooks_url(url)
    scheme = parts.scheme or "https"
    host = parts.hostname
    if host in {'ficbook.net', 'www.ficbook.net', 'ficbook.com', 'www.ficbook.com'}:
        host = host.replace("www.", "").replace("ficbook.com", "ficbook.net")
    if host in {"hogwartsnet.ru", "www.hogwartsnet.ru"}:
        scheme = "https"
        host = "hogwartsnet.ru"
    if host.replace("www.", "") == "litnet.com":
        scheme = "https"
        host = "litnet.com"
    if host.replace("www.", "") == "ranobelib.me":
        scheme = "https"
        host = "ranobelib.me"
    path = parts.path.rstrip("/")
    # Keep only parameters with meaning for the downloader. Never forward arbitrary
    # user parameters (including redirect destinations) to a source site.
    query = ''
    if host.removeprefix('www.') == 'archiveofourown.org':
        params = parse_qs(parts.query)
        query = urlencode({key: params[key][0] for key in ('view_adult', 'view_full_work') if key in params})
    if "readfic" in path:
        segments = [segment for segment in path.split("/") if segment]
        if len(segments) >= 2:
            path = f"/{segments[0]}/{segments[1]}"
            query = ""
    if host == "hogwartsnet.ru":
        params = parse_qs(parts.query)
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
    try:
        parts = _story_parts(value)
    except ValueError:
        return False
    host = parts.hostname.removeprefix('www.')
    if parts.hostname in ZAHLEB_HOSTS:
        try:
            normalize_zahleb_url(value)
            return True
        except ValueError:
            return False
    if parts.hostname in FIKBOOKS_HOSTS:
        try:
            normalize_fikbooks_url(value)
            return True
        except ValueError:
            return False
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
    host = (urlsplit(url).hostname or '').removeprefix('www.')
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
        "fikbooks.ru": "Fikbooks",
        "archiveofourown.org": "AO3",
        "www.wattpad.com": "Wattpad",
        "hogwartsnet.ru": "Hogwartsnet",
        "litnet.com": "Litnet",
        "ranobelib.me": "RanobeLIB",
        "zahleb.me": "Взахлёб",
    }
    return labels.get(site, site or "Сайт")
