#!/usr/bin/env python3
"""Developer helper: render the GTK window to a PNG without a screenshot tool.

Usage:
    python3 tools/ui_preview.py [monitor|settings] [output.png]

Uses the same offscreen snapshot technique in GTK; requires a working display
(the app itself needs one anyway).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk, Graphene  # noqa: E402

from aimail.ui.app import AIMailApplication  # noqa: E402
from aimail.util import AppPaths  # noqa: E402


def _find_vadjustment(widget: Gtk.Widget):
    if isinstance(widget, Gtk.ScrolledWindow):
        adjustment = widget.get_vadjustment()
        if adjustment is not None:
            return adjustment
    child = widget.get_first_child()
    while child is not None:
        found = _find_vadjustment(child)
        if found is not None:
            return found
        child = child.get_next_sibling()
    return None


def snapshot(widget: Gtk.Widget, path: Path, scale: int = 2) -> bool:
    width, height = widget.get_width(), widget.get_height()
    if width <= 0 or height <= 0:
        print("窗口还没有尺寸", file=sys.stderr)
        return False
    paintable = Gtk.WidgetPaintable.new(widget)
    snap = Gtk.Snapshot.new()
    paintable.snapshot(snap, width * scale, height * scale)
    node = snap.to_node()
    renderer = widget.get_native().get_renderer()
    rect = Graphene.Rect().init(0, 0, width * scale, height * scale)
    texture = renderer.render_texture(node, rect)
    path.parent.mkdir(parents=True, exist_ok=True)
    texture.save_to_png(str(path))
    print(f"saved {path} ({texture.get_width()}x{texture.get_height()})")
    return False


def main() -> int:
    page = sys.argv[1] if len(sys.argv) > 1 else "monitor"
    output = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "assets" / f"{page}.png"
    home = os.environ.setdefault("AI_MAIL_HOME", str(ROOT / ".devdata"))
    scheme = os.environ.get("AI_MAIL_PREVIEW_SCHEME", "")
    if scheme == "light":
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.PREFER_LIGHT)
    elif scheme == "dark":
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.PREFER_DARK)

    app = AIMailApplication(AppPaths.resolve(home))

    def capture_later() -> bool:
        window = app.get_active_window()
        if window is None:
            return True
        if page == "settings":
            window.show_page("settings")
            scroll = os.environ.get("AI_MAIL_PREVIEW_SCROLL")
            if scroll:
                def apply_scroll():
                    adjustment = _find_vadjustment(window.settings_page.page)
                    if adjustment is None:
                        return False
                    if scroll == "bottom":
                        adjustment.set_value(adjustment.get_upper())
                    else:
                        try:
                            adjustment.set_value(float(scroll))
                        except ValueError:
                            pass
                    return False
                GLib.timeout_add(300, apply_scroll)
        elif page == "monitor":
            window.show_page("monitor")
        if os.environ.get("AI_MAIL_PREVIEW_START") == "1":
            GLib.timeout_add(700, lambda: (window.toggle_monitor(), False)[1])
            GLib.timeout_add(3800, lambda: (snapshot(window, output), app.quit()))
        else:
            GLib.timeout_add(900, lambda: (snapshot(window, output), app.quit()))
        return False

    GLib.timeout_add(1400, capture_later)
    return int(app.run([]))


if __name__ == "__main__":
    raise SystemExit(main())
