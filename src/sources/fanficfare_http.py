"""FanFicFare transport using the application's verified, pinned HTTPS client."""

import json as json_module
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request

from fanficfare.exceptions import HTTPErrorFFF
from fanficfare.configurable import Configuration
from fanficfare.fetchers import SleepDecorator
from fanficfare.fetchers.base_fetcher import Fetcher, FetcherResponse

from src.core.limits import ResourceLimitError, image_is_safe, read_bounded, request_timeout
from src.sources.safe_http import UnsafeRequestError, build_safe_opener, require_https_url

SOURCE_HOSTS = {
    'ficbook.net': frozenset({'ficbook.net', 'www.ficbook.net'}),
    'archiveofourown.org': frozenset({'archiveofourown.org', 'www.archiveofourown.org',
                                    'download.archiveofourown.org'}),
    'wattpad.com': frozenset({'wattpad.com', 'www.wattpad.com', 'api.wattpad.com'}),
}


class SafeSourceConfiguration(Configuration):
    def __init__(self, sections, output_format, *, site):
        super().__init__(sections, output_format)
        self.site = site

    def get_fetcher(self, make_new=False):
        if self.fetcher is None or make_new:
            jar = self.fetcher.get_cookiejar() if self.fetcher is not None else None
            self.fetcher = SafeFanficFareFetcher(self.getConfig, self.getConfigList, self.site)
            if jar is not None:
                self.fetcher.set_cookiejar(jar)
            self.sleeper = SleepDecorator()
            self.sleeper.decorate_fetcher(self.fetcher)
        return self.fetcher


class SafeFanficFareFetcher(Fetcher):
    def __init__(self, get_config, get_config_list, site):
        super().__init__(get_config, get_config_list)
        self.allowed_hosts = SOURCE_HOSTS[site.removeprefix('www.')]

    def condition_url(self, url):
        if '\\' in url or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in url):
            raise UnsafeRequestError()
        parts = urlsplit(url)
        if parts.scheme == 'http' and parts.hostname in self.allowed_hosts:
            parts = parts._replace(scheme='https')
        url = urlunsplit(parts._replace(fragment=''))
        require_https_url(url)
        return url

    def request(self, method, url, headers=None, parameters=None, json=None):
        image = bool(headers and headers.get('Accept') == 'image/*')
        require_https_url(url, None if image else self.allowed_hosts)
        if method not in {'GET', 'POST'} or (image and method != 'GET'):
            raise UnsafeRequestError()
        headers = {name: value for name, value in (headers or {}).items()
                   if value and name.lower() not in {'cookie', 'authorization', 'proxy-authorization', 'host'}
                   and not (image and name.lower() in {'referer', 'origin'})}
        headers['Accept-Encoding'] = 'identity'
        data = None
        if json is not None:
            data = json_module.dumps(json).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        elif isinstance(parameters, (dict, list, tuple)):
            data = urlencode(parameters, doseq=True).encode('utf-8')
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        elif parameters is not None:
            data = parameters.encode('utf-8') if isinstance(parameters, str) else parameters
        opener = build_safe_opener(None if image else self.allowed_hosts,
                                   cookie_jar=None if image else self.get_cookiejar())
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with opener.open(request, timeout=request_timeout()) as response:
                content = read_bounded(response, 8 * 1024 * 1024 if image else None)
                if image and not image_is_safe(content):
                    raise ResourceLimitError('Обложка имеет небезопасный размер или формат.')
                result_url = response.geturl()
                require_https_url(result_url, None if image else self.allowed_hosts)
        except HTTPError as exc:
            with exc:
                body = read_bounded(exc)
            raise HTTPErrorFFF(url, exc.code, 'Source returned an HTTP error', body) from None
        return FetcherResponse(content, result_url, False,
                               json_module.loads(content) if json is not None else None)
