"""Which gamepad button does what.

The controller is **not a second input model**: every button here resolves to
something the shim already has -- a keyboard key it already binds, or the seek
the arrow keys already perform. A parallel set of gamepad handlers would be a
second implementation of the navigation ladder, and the second implementation
is where the drift happens.

Three kinds. :data:`KEY` is a *synthetic keypress*, so the d-pad follows the
UI from screen to screen without knowing any of it exists; :data:`SEEK` goes
to ``PlayerManager.kb_seek``, which is the *keyboard's* seek and not a number
of ours; :data:`NAV` hands a verb to the remote control's own ladder, for a
button whose meaning changes between the library and a playing video.

mpv names the face buttons by POSITION, not by label, and the two common pad
layouts disagree about where A is -- so "confirm is A" is not a statement this
code can make, and ``swap_confirm`` is the user telling us which pad is in
their hands. The sticks are asymmetric on purpose, and the spare buttons are
left free for ``input.conf``. All of that is derived in
docs/architecture.md section 5.

Pure data plus one function: no mpv, no settings import, no I/O.
"""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
KEY = 'key'
SELECT = object()
SEEK = 'seek'
NAV = 'nav'
DIRECTION_REPEAT = 0.15
PAGE_REPEAT = 0.35
SEEK_REPEAT = 0.4
NO_REPEAT = 0
CONFIRM_BUTTON = 'GAMEPAD_ACTION_DOWN'
BACK_BUTTON = 'GAMEPAD_ACTION_RIGHT'
DEFAULT_BINDS = (('GAMEPAD_DPAD_UP', KEY, 'UP', DIRECTION_REPEAT), ('GAMEPAD_DPAD_DOWN', KEY, 'DOWN', DIRECTION_REPEAT), ('GAMEPAD_DPAD_LEFT', KEY, 'LEFT', DIRECTION_REPEAT), ('GAMEPAD_DPAD_RIGHT', KEY, 'RIGHT', DIRECTION_REPEAT), ('GAMEPAD_LEFT_STICK_UP', KEY, 'UP', DIRECTION_REPEAT), ('GAMEPAD_LEFT_STICK_DOWN', KEY, 'DOWN', DIRECTION_REPEAT), ('GAMEPAD_LEFT_STICK_LEFT', KEY, 'LEFT', DIRECTION_REPEAT), ('GAMEPAD_LEFT_STICK_RIGHT', KEY, 'RIGHT', DIRECTION_REPEAT), (CONFIRM_BUTTON, KEY, SELECT, NO_REPEAT), (BACK_BUTTON, KEY, 'ESC', NO_REPEAT), ('GAMEPAD_ACTION_LEFT', KEY, 'SPACE', NO_REPEAT), ('GAMEPAD_START', NAV, 'menu', NO_REPEAT), ('GAMEPAD_LEFT_SHOULDER', KEY, 'PGUP', PAGE_REPEAT), ('GAMEPAD_RIGHT_SHOULDER', KEY, 'PGDWN', PAGE_REPEAT), ('GAMEPAD_RIGHT_STICK_UP', SEEK, 'up', SEEK_REPEAT), ('GAMEPAD_RIGHT_STICK_DOWN', SEEK, 'down', SEEK_REPEAT), ('GAMEPAD_RIGHT_STICK_LEFT', SEEK, 'left', SEEK_REPEAT), ('GAMEPAD_RIGHT_STICK_RIGHT', SEEK, 'right', SEEK_REPEAT))

def bindings(swap_confirm=False, select_key='ENTER'):
    """The binding table as a list of ``[key, kind, argument, repeat]``.

    ``select_key`` is what Confirm sends: the key that activates the focused
    control, which the user may remap. It is a PARAMETER and not a settings
    read, so this module stays pure (see the header) and so a caller cannot
    quietly leave the pad naming a different key from the keyboard -- the
    failure mode #717's repair is most likely to produce, since a test that
    only presses the keyboard cannot see it.

    Lists rather than tuples because this is JSON on the way to the renderer,
    and a tuple would come back as a list anyway -- pinning the shape here
    keeps the tests honest about what Lua actually receives. The renderer
    reads all four positionally, so the arity is part of the contract.
    """
    swapped = {}
    if swap_confirm:
        swapped = {CONFIRM_BUTTON: BACK_BUTTON, BACK_BUTTON: CONFIRM_BUTTON}
    out = []
    for key, kind, arg, rate in DEFAULT_BINDS:
        out.append([swapped.get(key, key), kind, select_key if arg is SELECT else arg, rate])
    return out
