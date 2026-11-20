from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
from personal_cinema import __version__
APP_NAME = 'personal-cinema'
USER_APP_NAME = 'Personal Cinema'
DESKTOP_ID = 'PersonalCinema.Desktop'
CLIENT_VERSION = __version__
UPSTREAM_CLIENT_VERSION = '3.3.0'
OFFLINE_SERVER_UUID = 'offline'
OFFLINE_PLAYLIST_PREFIX = 'offline:playlist:'

def offline_playlist_id(playlist_id, content_server_id):
    """The offline library's id for one server's copy of a playlist."""
    return '%s%s:%s' % (OFFLINE_PLAYLIST_PREFIX, content_server_id or '', playlist_id)

def split_offline_playlist_id(item_id):
    """``(playlist_id, content_server_id)`` for an offline playlist id.

    ``(item_id, None)`` for anything else, so a caller can pass any id it
    holds: an online DTO's id comes back unchanged, which is what every
    caller wants to do with it anyway.
    """
    if not item_id or not item_id.startswith(OFFLINE_PLAYLIST_PREFIX):
        return (item_id, None)
    rest = item_id[len(OFFLINE_PLAYLIST_PREFIX):]
    scope, _, playlist_id = rest.partition(':')
    return (playlist_id or item_id, scope or None)
CONNECT_UNREACHABLE = 'unreachable'
CONNECT_SIGNED_OUT = 'signed_out'
CONNECT_BUSY = 'busy'
REAUTH_WRONG_SERVER = 'wrong_server'
USER_AGENT = 'PersonalCinema/%s (Jellyfin-MPV-Shim/%s)' % (CLIENT_VERSION, UPSTREAM_CLIENT_VERSION)
CAPABILITIES = {'PlayableMediaTypes': ['Video', 'Audio', 'Photo'], 'SupportsMediaControl': True, 'SupportsPersistentIdentifier': True, 'SupportedCommands': ['MoveUp', 'MoveDown', 'MoveLeft', 'MoveRight', 'Select', 'Back', 'ToggleFullscreen', 'GoHome', 'GoToSettings', 'ToggleContextMenu', 'GoToSearch', 'TakeScreenshot', 'VolumeUp', 'VolumeDown', 'ToggleMute', 'SetAudioStreamIndex', 'SetSubtitleStreamIndex', 'Mute', 'Unmute', 'SetVolume', 'DisplayContent', 'Play', 'Playstate', 'PlayNext', 'PlayMediaSource']}
