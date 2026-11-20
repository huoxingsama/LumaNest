"""Different redirect policies for credential APIs and media CDN requests."""
from __future__ import annotations

import urllib.parse
import urllib.request


class CredentialRedirectError(ValueError):
    """Safe to display: deliberately excludes server-controlled URL/body text."""


class NoCredentialRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        fp.close()
        raise CredentialRedirectError('认证接口返回重定向，已停止发送凭据；请配置服务最终地址')


def _origin(url):
    value = urllib.parse.urlsplit(url)
    if (value.scheme not in ('http', 'https') or not value.hostname or value.username or value.password
            or any(ord(ch) < 32 for ch in url) or '\\' in url):
        raise CredentialRedirectError('媒体跳转地址无效，已停止请求')
    try: port = value.port or (443 if value.scheme == 'https' else 80)
    except ValueError:
        raise CredentialRedirectError('媒体跳转地址无效，已停止请求') from None
    return value.scheme, value.hostname.lower(), port


class MediaRedirect(urllib.request.HTTPRedirectHandler):
    max_redirections = 5
    max_repeats = 2

    def redirect_request(self, request, fp, code, msg, headers, newurl):
        try:
            original, destination = _origin(request.full_url), _origin(newurl)
            if original[0] == 'https' and destination[0] != 'https':
                raise CredentialRedirectError('媒体跳转降低了传输保护，已停止请求')
        except CredentialRedirectError:
            fp.close()
            raise
        redirected = super().redirect_request(request, fp, code, msg, headers, newurl)
        if redirected is not None and original != destination:
            # Keep only non-authentication headers needed for byte-range playback.
            # A future custom auth header cannot silently escape this allowlist.
            allowed = {'range', 'if-range', 'user-agent', 'accept', 'accept-encoding', 'accept-language',
                       'cache-control', 'if-modified-since', 'if-none-match'}
            redirected.headers = {key: value for key, value in redirected.headers.items() if key.lower() in allowed}
            redirected.unredirected_hdrs = {}
        return redirected


def _opener(policy, handlers):
    if any(isinstance(handler, urllib.request.HTTPRedirectHandler) for handler in handlers):
        raise ValueError('认证与媒体跳转策略不能被其他重定向处理器覆盖')
    return urllib.request.build_opener(*handlers, policy)


def credential_opener(*handlers):
    """Service login, administration and JSON APIs must not redirect."""
    return _opener(NoCredentialRedirect(), handlers)


def media_opener(*handlers):
    """Allow CDN playback redirects without forwarding the service's secrets."""
    return _opener(MediaRedirect(), handlers)
