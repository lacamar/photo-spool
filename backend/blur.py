"""Compositor-blur detection (progressive enhancement).

No Python bindings exist for KWindowSystem/KWindowEffects, so per the spec
we talk to the Wayland protocol directly -- but only for *detection*.
Actually requesting a blur region requires handing the compositor our
window's `wl_surface`, and PySide6 (6.11) exposes no per-window Wayland
native-interface binding to obtain that pointer from Python (only
`QNativeInterface.QWaylandApplication`, which is display-wide, not
per-surface) -- generating one would mean shipping a small C extension,
which is out of proportion for an effect the target compositor (niri)
doesn't implement anyway.

So: this module opens its own short-lived Wayland connection purely to ask
the compositor "do you advertise org_kde_kwin_blur_manager?" and reports
the answer. The window always renders semi-transparent (see Main.qml); if
this returns True a future compositor-specific integration could request
an actual blur region, and if False (the honest answer on niri today) the
translucent background alone is what the user sees -- exactly the
"progressive enhancement, never break anything" behaviour the spec asks
for.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

BLUR_MANAGER_INTERFACE = "org_kde_kwin_blur_manager"


def compositor_supports_blur(timeout_s: float = 0.5) -> bool:
    try:
        from pywayland.client import Display
    except ImportError:
        return False

    found = False

    def handle_global(registry, id_, interface_name, version):
        nonlocal found
        if interface_name == BLUR_MANAGER_INTERFACE:
            found = True

    display = None
    try:
        display = Display()
        display.connect()
        registry = display.get_registry()
        registry.dispatcher["global"] = handle_global
        display.dispatch(block=True)
        display.roundtrip()
    except Exception:
        logger.info("Wayland blur-protocol detection failed; using translucent fallback", exc_info=True)
        return False
    finally:
        if display is not None:
            try:
                display.disconnect()
            except Exception:
                pass
    return found
