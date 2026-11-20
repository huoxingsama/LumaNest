"""Wall-clock formatting, in one place because three subsystems show one.

The Live TV guide and its air times, the detail page's "Ends at" and the
playback HUD's "Ends at" all print a time of day, and they were three
independent ``strftime("%H:%M")`` calls. That is fine while there is one
answer; it stops being fine the moment the answer is a setting, because the
third call site is the one nobody remembers.

Not locale-driven. Python only consults ``LC_TIME`` after a ``setlocale`` this
app never makes, so ``%p`` would be empty or English regardless of where the
user is -- and reaching for the platform's own idea of the format is a
Windows/POSIX split for a two-line function. The user says which they want.
"""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
import datetime
from ..conf import settings
from ..i18n import _p

def clock(when):
    """``when`` (an aware or naive ``datetime``) as a wall clock, or "".

    Reads the setting per call rather than caching it: the control applies
    live, and a value read once at import is a control that does nothing
    until the app is restarted.
    """
    if when is None:
        return ''
    if not settings.clock_12h:
        return when.strftime('%H:%M')
    return _p('12-hour clock', '{time} {period}').format(time='%d:%02d' % (when.hour % 12 or 12, when.minute), period=_p('12-hour clock', 'AM') if when.hour < 12 else _p('12-hour clock', 'PM'))

def clock_epoch(timestamp):
    """:func:`clock` for a POSIX timestamp, in local time.

    The HUD works in seconds-since-epoch because that is what it adds a
    remaining runtime to; converting here keeps it from growing a datetime
    import for one line.
    """
    return clock(datetime.datetime.fromtimestamp(timestamp))
