from contextlib import contextmanager
import ctypes
import ctypes.util
from functools import wraps
import json
import os


@contextmanager
def x11_display():
    library = ctypes.util.find_library("X11")
    if not library:
        yield None, None
        return
    xlib = ctypes.CDLL(library)
    xlib.XOpenDisplay.restype = ctypes.c_void_p
    xlib.XOpenDisplay.argtypes = [ctypes.c_char_p]
    xlib.XCloseDisplay.restype = ctypes.c_int
    xlib.XCloseDisplay.argtypes = [ctypes.c_void_p]
    display = xlib.XOpenDisplay(None)
    try:
        yield xlib, display
    finally:
        if display:
            xlib.XCloseDisplay(display)


def net_workareas():
    """Read _NET_WORKAREA from the X11 root window as (x, y, width, height).

    GTK's GdkMonitor.get_workarea() reports the full monitor geometry under
    XWayland, so read the taskbar-aware workarea straight from the X root
    window instead. Returns [] when the property is unavailable.
    """
    with x11_display() as (xlib, display):
        if not display:
            return []
        xlib.XDefaultRootWindow.restype = ctypes.c_ulong
        xlib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        root = xlib.XDefaultRootWindow(display)

        xlib.XInternAtom.restype = ctypes.c_ulong
        xlib.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        atom = xlib.XInternAtom(display, b"_NET_WORKAREA", 0)

        actual_type = ctypes.c_ulong()
        actual_format = ctypes.c_int()
        nitems = ctypes.c_ulong()
        bytes_after = ctypes.c_ulong()
        prop = ctypes.POINTER(ctypes.c_ubyte)()

        xlib.XGetWindowProperty.restype = ctypes.c_int
        xlib.XGetWindowProperty.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_long,
            ctypes.c_long, ctypes.c_int, ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte)),
        ]
        status = xlib.XGetWindowProperty(
            display, root, atom, 0, 1024, 0, 0,
            ctypes.byref(actual_type), ctypes.byref(actual_format),
            ctypes.byref(nitems), ctypes.byref(bytes_after), ctypes.byref(prop),
        )
        if status != 0 or actual_format.value != 32 or nitems.value < 4:
            return []
        data = ctypes.cast(prop, ctypes.POINTER(ctypes.c_long))
        values = [data[i] for i in range(nitems.value)]
        return [tuple(values[i:i + 4]) for i in range(0, len(values) - 3, 4)]


def pointer_position():
    """Read X11's pointer position; it can be stale over native Wayland apps."""
    with x11_display() as (xlib, display):
        if not display:
            return None
        xlib.XDefaultRootWindow.restype = ctypes.c_ulong
        xlib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        root = xlib.XDefaultRootWindow(display)

        xlib.XQueryPointer.restype = ctypes.c_int
        xlib.XQueryPointer.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_uint),
        ]
        root_return = ctypes.c_ulong()
        child_return = ctypes.c_ulong()
        root_x = ctypes.c_int()
        root_y = ctypes.c_int()
        win_x = ctypes.c_int()
        win_y = ctypes.c_int()
        mask_return = ctypes.c_uint()
        if not xlib.XQueryPointer(
            display, root, ctypes.byref(root_return), ctypes.byref(child_return),
            ctypes.byref(root_x), ctypes.byref(root_y),
            ctypes.byref(win_x), ctypes.byref(win_y), ctypes.byref(mask_return),
        ):
            return None
        return root_x.value, root_y.value


def x11_screen_size():
    with x11_display() as (xlib, display):
        if not display:
            return None
        xlib.XDefaultScreen.restype = ctypes.c_int
        xlib.XDefaultScreen.argtypes = [ctypes.c_void_p]
        screen = xlib.XDefaultScreen(display)
        for func in (xlib.XDisplayWidth, xlib.XDisplayHeight):
            func.restype = ctypes.c_int
            func.argtypes = [ctypes.c_void_p, ctypes.c_int]
        return xlib.XDisplayWidth(display, screen), xlib.XDisplayHeight(display, screen)


def gnome_workarea():
    """Get the compositor's pointer monitor even when XWayland lacks motion."""
    from gi.repository import Gio, GLib

    try:
        reply = Gio.bus_get_sync(Gio.BusType.SESSION, None).call_sync(
            "org.gnome.Shell", "/local/KittyDropdown/Monitor",
            "local.KittyDropdown.Monitor", "GetPointerWorkarea", None,
            GLib.VariantType.new("(iiiiii)"), Gio.DBusCallFlags.NO_AUTO_START, 300, None,
        )
    except GLib.Error:
        return None
    x, y, width, height, stage_width, stage_height = reply.unpack()
    root_size = x11_screen_size()
    if root_size is None or min(width, height, stage_width, stage_height) <= 0:
        return None
    # Mutter uses a uniform integer scale from stage to XWayland coordinates.
    root_width, root_height = root_size
    scale, remainder = divmod(root_width, stage_width)
    if scale < 1 or remainder or root_height != stage_height * scale:
        return None
    return x * scale, y * scale, width * scale, height * scale


def workarea_for(geometry, workareas):
    """Intersect a monitor geometry with the _NET_WORKAREA entries.

    In join mode (one X screen spanning several displays, e.g. XWayland)
    _NET_WORKAREA can be a single rectangle covering the whole desktop.
    Matching by center point would then return that whole desktop and the
    panel would spread across every monitor. Intersect the monitor with
    each entry instead and return the one sharing the largest area with it.
    """
    best = None
    for x, y, width, height in workareas:
        ix = max(geometry.x, x)
        iy = max(geometry.y, y)
        iw = min(geometry.x + geometry.width, x + width) - ix
        ih = min(geometry.y + geometry.height, y + height) - iy
        if iw > 0 and ih > 0 and (best is None or iw * ih > best[2] * best[3]):
            best = (ix, iy, iw, ih)
    return best


def monitor_geometry():
    if os.environ.get("XDG_SESSION_TYPE") == "wayland":
        area = gnome_workarea()
        if area is not None:
            return area

    import gi

    gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk

    display = Gdk.Display.get_default()
    if display is None:
        return None
    position = pointer_position()
    if position is None:
        # Wayland-native fallback: only useful when not running through XWayland.
        pointer = display.get_default_seat().get_pointer()
        if pointer is None:
            return None
        _, x, y = pointer.get_position()
        position = (x, y)
    monitor = display.get_monitor_at_point(*position)
    if monitor is None:
        return None
    geometry = monitor.get_geometry()
    area = workarea_for(geometry, net_workareas())
    if area is None:
        workarea = monitor.get_workarea()
        area = (workarea.x, workarea.y, workarea.width, workarea.height)
    return area


def on_load(boss, data):
    from kittens.panel import main as panel
    from kitty.fast_data_types import os_window_is_invisible

    original = panel.handle_single_instance_command
    if getattr(original, "_kitty_dropdown_monitor", False):
        return

    @wraps(original)
    def handle(boss, *args, **kwargs):
        result = original(boss, *args, **kwargs)
        # Native X11 show/config reload restores primary-monitor placement.
        for manager in boss.os_window_map.values():
            controls = getattr(manager, "_dropdown_controls", None)
            if controls is not None and not os_window_is_invisible(manager.os_window_id):
                controls.place()
        return result

    handle._kitty_dropdown_monitor = True
    panel.handle_single_instance_command = handle


if __name__ == "__main__":
    print(json.dumps(monitor_geometry()))
