from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
from threading import Event, Thread
from typing import Optional
POLL_INTERVAL_SECS = 0.5

def observe(instance, name, handler):
    """Register ``handler`` for property ``name`` on either backend.

    Deliberately NOT python-mpv's ``property_observer`` decorator. That writes
    an ``unobserve_mpv_properties`` attribute onto the callback it is given,
    and a **bound method has no __dict__ to write it to** -- it raises
    AttributeError. jsonipc and the test fake both accept one, so a handler
    that stops being a plain closure breaks on exactly one backend, at
    runtime, and nowhere else.

    Backend picked by class capability, like ``wait_property`` below and for
    the same reason: libmpv's ``__getattr__`` turns an unknown *instance*
    attribute into a property read, so an instance-level hasattr would be
    both wrong and wasteful.

    Returns the token :func:`unobserve` needs (jsonipc hands back an observer
    id; libmpv identifies a registration by name and handler, so None).

    Here rather than on ``PlayerManager`` because importing ``player`` builds
    a real mpv as a side effect, and the one place that most needs to check
    this dispatch against a real backend is a test that must not.
    """
    if hasattr(type(instance), 'bind_property_observer'):
        return instance.bind_property_observer(name, handler)
    instance.observe_property(name, handler)
    return None

def unobserve(instance, name, handler, token=None):
    """Undo :func:`observe`.

    Nothing in the app calls this -- mpv is torn down whole, handle and
    observers together. A caller that outlives its handle's observers has to,
    though: libmpv segfaults the interpreter on the way out otherwise.
    """
    if hasattr(type(instance), 'bind_property_observer'):
        if token is not None:
            instance.unbind_property_observer(token)
    else:
        instance.unobserve_property(name, handler)

def wait_property(instance, name: str, cond=lambda x: True, timeout: Optional[int]=None, skip_initial: bool=False, abort: Optional[Event]=None, satisfied_by: Optional[Event]=None):
    """Block until MPV property ``name`` reports a value satisfying ``cond``.

    Works with both backends; the backend is picked by class capability, so
    this carries no global state and is testable with a fake ``instance``.

    ``skip_initial`` drops a value belonging to the *previous* file -- both
    backends deliver the property's current value the instant an observer
    registers, and with a prior file still loaded that value is the old one.

    The wait is **poll-assisted on its own daemon thread**, and that is not
    redundancy: the external backend's IPC pipeline has been seen in the field
    to drop property-change notifications, which turned a fine playback start
    into a hard "no duration" timeout that killed the session. **Do not
    simplify it away.** Its own thread because a jsonipc property read blocks
    for up to 120s, which would stretch ``timeout`` past being a hard bound.

    ``abort`` ends the wait as a failure, ``satisfied_by`` as a success -- the
    latter for a stream that will never report a duration at all. Both are
    observed by the poll thread, so they land within one poll interval.

    The residual race, and the full argument for each parameter:
    docs/mpv-backends.md section 9.
    """
    event = Event()
    satisfied = False
    skip = False
    stale_value = None
    if skip_initial:
        try:
            stale_value = getattr(instance, name)
            skip = cond(stale_value)
        except Exception:
            skip = False

    def handler(_name, value):
        nonlocal skip, satisfied
        if skip:
            skip = False
            if value == stale_value:
                return
        if cond(value):
            satisfied = True
            event.set()
    use_ext_mpv = hasattr(type(instance), 'bind_property_observer')
    if use_ext_mpv:
        observer_id = instance.bind_property_observer(name, handler)
    else:
        instance.observe_property(name, handler)
    stop_poll = Event()

    def poller():
        nonlocal satisfied
        while not stop_poll.wait(POLL_INTERVAL_SECS):
            if abort is not None and abort.is_set():
                event.set()
                return
            if satisfied_by is not None and satisfied_by.is_set():
                satisfied = True
                event.set()
                return
            try:
                value = getattr(instance, name)
            except Exception:
                continue
            if cond(value) and (not (skip_initial and value == stale_value)):
                satisfied = True
                event.set()
                return
    poll_thread = Thread(target=poller, daemon=True, name='wait-property-poll')
    poll_thread.start()
    event.wait(timeout=timeout)
    stop_poll.set()
    if use_ext_mpv:
        instance.unbind_property_observer(observer_id)
    else:
        instance.unobserve_property(name, handler)
    return satisfied
