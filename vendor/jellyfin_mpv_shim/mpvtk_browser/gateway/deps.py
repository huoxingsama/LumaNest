"""The one module in this package that binds a foreign singleton at import.

Everything else imports its collaborator *inside* the call — see
``__init__.py`` on why that discipline exists. ``clientManager`` is the
exception: it is read by five of the twelve domains, and

**it is rebound by tests.** ``tests/test_player_controller.py`` swaps it for a
``BrokenService`` to sweep the whole gateway's failure contract. A binding
per submodule would mean patching five names to run one sweep, and — worse —
patching *four of five* would look like it worked. One module, one name, one
patch target.

So submodules do ``from . import deps`` and read ``deps.clientManager``
rather than importing the name. That indirection is the point: rebinding the
attribute here is visible to every reader.
"""
from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
from ...clients import clientManager
