"""Home-screen section layout, compatible with jellyfin-web's.

The layout lives server-side in DisplayPreferences, one string per slot in
``CustomPrefs`` under ``homesection0``..``homesection9``. The document's id
and client namespace are load-bearing and not obvious:
``docs/jellyfin-api-notes.md`` section 7.1.

Everything here is pure: no network, no settings singleton. The I/O lives in
repository.LibrarySource, which reads/writes the DTO and hands the CustomPrefs
dict to resolve_layout().

Two rules from jellyfin-web that are easy to get wrong, and both are load
bearing for interop:

* An absent or empty slot means "use *this slot's* default", NOT "none". Only
  the literal string "none" blanks a slot. jellyfin-web's settings UI rewrites
  the default option's value to "" before saving, so a user who never touched
  the screen and a user who explicitly picked the default are indistinguishable
  on the wire — both round-trip through the per-slot default.
* A stored "folders" is a pre-10.x alias for smalllibrarytiles.
"""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
from ..i18n import _, _p
DISPLAY_PREFS_CLIENT = 'emby'
SLOT_COUNT = 10
NONE = 'none'
LIBRARIES = 'smalllibrarytiles'
LIBRARY_BUTTONS = 'librarybuttons'
RESUME = 'resume'
RESUME_AUDIO = 'resumeaudio'
RESUME_BOOK = 'resumebook'
NEXT_UP = 'nextup'
LATEST = 'latestmedia'
LIVE_TV = 'livetv'
ACTIVE_RECORDINGS = 'activerecordings'
DEFAULT_LAYOUT = (LIBRARIES, RESUME, RESUME_AUDIO, RESUME_BOOK, LIVE_TV, NEXT_UP, LATEST, NONE, NONE, NONE)
SUPPORTED = frozenset({NONE, LIBRARIES, RESUME, RESUME_AUDIO, RESUME_BOOK, NEXT_UP, LATEST, LIVE_TV, ACTIVE_RECORDINGS})
STAGE = {LIBRARIES: 'local', RESUME: 'primary', RESUME_AUDIO: 'primary', RESUME_BOOK: 'primary', NEXT_UP: 'primary', LIVE_TV: 'primary', ACTIVE_RECORDINGS: 'primary', LATEST: 'latest'}

def section_labels():
    """(value, label) for every section offered in the settings dropdowns.

    A function, not a constant: these are translated, and i18n is initialised
    after import on some paths.
    """
    return [(NONE, _p('home screen section type', 'None')), (LIBRARIES, _('My Media')), (RESUME, _('Continue Watching')), (RESUME_AUDIO, _('Continue Listening')), (RESUME_BOOK, _('Continue Reading')), (NEXT_UP, _('Next Up')), (LATEST, _('Recently Added')), (LIVE_TV, _('Live TV')), (ACTIVE_RECORDINGS, _('Active Recordings'))]

def default_section(slot: int) -> str:
    """The default type for one slot, or "none" past the end."""
    if 0 <= slot < len(DEFAULT_LAYOUT):
        return DEFAULT_LAYOUT[slot]
    return NONE

def resolve_layout(custom_prefs) -> list:
    """CustomPrefs dict -> the ordered list of SLOT_COUNT section types.

    Unknown/unsupported values are kept as-is; callers skip what they cannot
    draw. This is deliberate — see SUPPORTED.
    """
    prefs = custom_prefs or {}
    layout = []
    for slot in range(SLOT_COUNT):
        value = prefs.get('homesection%d' % slot)
        value = '' if value is None else str(value).strip()
        if value == 'folders':
            value = DEFAULT_LAYOUT[0]
        layout.append(value or default_section(slot))
    return layout

def layout_to_prefs(layout) -> dict:
    """The ordered section list -> the CustomPrefs keys to write.

    Writes "" for a slot holding its own default, which is what jellyfin-web
    stores and what keeps a never-customised layout from being pinned to
    today's defaults if the server's change later.
    """
    prefs = {}
    for slot in range(SLOT_COUNT):
        value = layout[slot] if slot < len(layout) else default_section(slot)
        value = str(value or NONE)
        prefs['homesection%d' % slot] = '' if value == default_section(slot) else value
    return prefs

def stages_for(layout):
    """The fetch stages a layout actually needs, e.g. {"primary", "latest"}.

    Lets the loader skip a round trip entirely when no slot asks for it.
    """
    return {STAGE[s] for s in layout if s in STAGE}
