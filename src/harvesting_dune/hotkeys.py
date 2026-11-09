# Hotkeys: global Ctrl+Alt shortcuts delivered into the event loop.
"""Global hotkeys (Win32 RegisterHotKey) delivered into the asyncio loop.

Ctrl+Alt+Right / Left : next / previous resource
Ctrl+Alt+Up           : cycle vehicle (auto, sandcrawler, buggy, sandbike, ornithopter)
Ctrl+Alt+Down         : recommendations on / off
Ctrl+Alt+L            : directional lines on / off (drawn only when one resource is shown)
Ctrl+Alt+M            : companion minimap on / off, in every activity (on foot, combat, driving, building)
Ctrl+Alt+N            : farm route: skip to the next stop (like Ctrl+Alt+M, mouse software can put it on a button)
Ctrl+Alt+P            : drop a guild pin with your preset label at your companion destination, else where you stand

RegisterHotKey only listens for the key combination; it does not read or inject
input into the game.
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import sys
import threading
from collections.abc import Callable
from ctypes import wintypes

log = logging.getLogger(__name__)

MOD_ALT, MOD_CONTROL, MOD_NOREPEAT = 0x0001, 0x0002, 0x4000
VK_LEFT, VK_UP, VK_RIGHT, VK_DOWN = 0x25, 0x26, 0x27, 0x28
VK_L, VK_M, VK_N, VK_P = 0x4C, 0x4D, 0x4E, 0x50
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012
BINDINGS = {
    1: (VK_RIGHT, "next_resource"),
    2: (VK_LEFT, "prev_resource"),
    3: (VK_UP, "cycle_vehicle"),
    4: (VK_DOWN, "toggle"),
    5: (VK_P, "object_pin"),
    6: (VK_L, "lines"),
    7: (VK_M, "minimap"),
    8: (VK_N, "route_skip"),
}


def start(loop: asyncio.AbstractEventLoop, on_action: Callable[[str], None]) -> Callable[[], None] | None:
    if sys.platform != "win32":
        return None
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    ready = threading.Event()
    state: dict = {}

    def run() -> None:
        state["tid"] = kernel32.GetCurrentThreadId()
        registered = []
        for hid, (vk, name) in BINDINGS.items():
            if user32.RegisterHotKey(None, hid, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, vk):
                registered.append(hid)
            else:
                log.warning("hotkey for %s is taken by another program", name)
        ready.set()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY and msg.wParam in BINDINGS:
                loop.call_soon_threadsafe(on_action, BINDINGS[msg.wParam][1])
        for hid in registered:
            user32.UnregisterHotKey(None, hid)

    t = threading.Thread(target=run, name="hotkeys", daemon=True)
    t.start()
    ready.wait(2)

    def stop() -> None:
        if "tid" in state:
            user32.PostThreadMessageW(state["tid"], WM_QUIT, 0, 0)

    return stop
