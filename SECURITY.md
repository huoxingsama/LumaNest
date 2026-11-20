# Security and privacy

The settings server binds only to loopback. A short-lived launch token is
exchanged for an HttpOnly session cookie; writes additionally require a CSRF
token. Host and Origin checks reject requests from unrelated websites.
Do not expose this administration server directly to the Internet.

Credentials and native-player profiles use Windows DPAPI for the current user.
There is no plaintext fallback. A backup containing credentials remains tied to
the original Windows user and machine; configuration export omits credentials
and private paths. Reauthorize services on a different machine.

Only processes registered by this instance may be stopped. Existing Jellyfin,
OpenList and network installations are never implicitly taken over. Source
write permissions are explicit and scoped to selected folders. Media originals
are excluded from uninstall and backup operations.

If reporting a problem, attach the settings page's redacted diagnostics, version
and reproducible steps. Do not post API keys, cookies, signed stream URLs, raw
credential files, full runtime folders, or personal viewing history in issues.
Use the repository's private security reporting channel when its owner enables
one; until then, arrange a private channel with the maintainer before sharing a
sensitive report.
