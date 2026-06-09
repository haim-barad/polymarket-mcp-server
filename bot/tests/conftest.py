"""Conftest for bot tests: handle stdlib-shadowing local modules.

The bot has a module named `signal` at `bot/signal.py`. During pytest's
own bootstrap, the stdlib `signal` module is imported (for SIGINT
handling, etc.) and cached in `sys.modules`. If we leave it there, a
test that does `from signal import evaluate_market` will resolve to the
cached stdlib module (which has no such attribute) and fail.

We work around this by:
  1. Pushing `bot/` to the front of `sys.path`.
  2. Removing `signal` from `sys.modules` (so the next import re-scans
     sys.path and finds our `bot/signal.py`).
  3. Pre-importing the local `signal` module so subsequent
     `from signal import evaluate_market` statements inside tests
     resolve to our copy.

This keeps test source code unchanged (`from signal import
evaluate_market`) while still letting us name the module `signal`.
"""
import importlib
import sys
from pathlib import Path

_BOT_DIR_STR = str(Path(__file__).resolve().parents[1])
if _BOT_DIR_STR not in sys.path:
    sys.path.insert(0, _BOT_DIR_STR)
else:
    # Move it to the front so it wins over stdlib paths.
    sys.path.remove(_BOT_DIR_STR)
    sys.path.insert(0, _BOT_DIR_STR)

# Force re-import of any bot-local module that shadows a stdlib name.
for _shadowed in ("signal",):
    if _shadowed in sys.modules:
        mod = sys.modules[_shadowed]
        origin = getattr(mod, "__file__", "") or ""
        # If the cached module is the stdlib one, evict it.
        if "/lib/python" in origin or origin == "" or "/python3" in origin and _BOT_DIR_STR not in origin:
            del sys.modules[_shadowed]
    try:
        importlib.import_module(_shadowed)
    except Exception:
        # If the local module is missing, leave the (stdlib) entry alone
        # and let the test produce its own clear error.
        pass
