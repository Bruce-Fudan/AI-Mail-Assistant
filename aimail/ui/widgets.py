"""Reusable GTK widgets and small async helpers for the UI."""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Callable, TypeVar

from gi.repository import GLib, Gtk

T = TypeVar("T")


def run_async(func: Callable[[], T], on_success: Callable[[T], None], on_error: Callable[[Exception], None]) -> None:
    """Run blocking I/O off the GTK main loop and deliver callbacks on it."""

    def worker() -> None:
        try:
            result = func()
        except Exception as exc:  # noqa: BLE001 - shown to the user
            GLib.idle_add(_deliver_error, on_error, exc)
        else:
            GLib.idle_add(_deliver_success, on_success, result)

    threading.Thread(target=worker, daemon=True).start()


def _deliver_success(callback: Callable[[T], None], result: T) -> bool:
    callback(result)
    return False


def _deliver_error(callback: Callable[[Exception], None], exc: Exception) -> bool:
    callback(exc)
    return False


class StatusDot(Gtk.Label):
    def __init__(self):
        super().__init__(label="●")
        self.add_css_class("status-dot")
        self.set_state("idle")

    def set_state(self, state: str) -> None:
        for name in ("idle", "running", "stopping", "error"):
            self.remove_css_class(f"dot-{name}")
        self.add_css_class(f"dot-{state}")


class StatusPill(Gtk.Label):
    def __init__(self, text: str = "已停止"):
        super().__init__(label=text)
        self.add_css_class("status-pill")
        self.set_state("idle", text)

    def set_state(self, state: str, text: str) -> None:
        for name in ("idle", "running", "stopping", "error"):
            self.remove_css_class(f"pill-{name}")
        self.add_css_class(f"pill-{state}")
        self.set_text(text)


class StatCard(Gtk.Box):
    def __init__(self, caption: str):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.add_css_class("stat-card")
        self.set_hexpand(True)
        self.value_label = Gtk.Label(label="0", xalign=0)
        self.value_label.add_css_class("stat-value")
        self.caption_label = Gtk.Label(label=caption, xalign=0)
        self.caption_label.add_css_class("stat-caption")
        self.append(self.value_label)
        self.append(self.caption_label)

    def set_value(self, value: str) -> None:
        self.value_label.set_text(value)


class LogView(Gtk.Frame):
    LEVEL_TAGS = {
        "info": "info",
        "success": "success",
        "warning": "warning",
        "error": "error",
        "debug": "debug",
    }

    def __init__(self, max_lines: int = 800):
        super().__init__()
        self.add_css_class("log-frame")
        self.max_lines = max_lines
        self.text_view = Gtk.TextView()
        self.text_view.set_editable(False)
        self.text_view.set_cursor_visible(False)
        self.text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.text_view.set_top_margin(10)
        self.text_view.set_bottom_margin(10)
        self.text_view.set_left_margin(12)
        self.text_view.set_right_margin(12)
        self.text_view.add_css_class("log-view")
        self.buffer = self.text_view.get_buffer()
        self._create_tags()
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(self.text_view)
        scroller.set_min_content_height(250)
        scroller.set_vexpand(True)
        self.set_child(scroller)
        self.set_hexpand(True)

    def _create_tags(self) -> None:
        colors = {
            "time": "#8a8a8a",
            "success": "#26a269",
            "warning": "#e5a50a",
            "error": "#e01b24",
            "debug": "#8a8a8a",
        }
        for name, color in colors.items():
            tag = Gtk.TextTag.new(name)
            tag.set_property("foreground", color)
            self.buffer.get_tag_table().add(tag)

    def append(self, message: str, level: str = "info", timestamp: datetime | None = None) -> None:
        timestamp = timestamp or datetime.now()
        prefix = f"{timestamp.strftime('%H:%M:%S')}  "
        tag_name = self.LEVEL_TAGS.get(level, "info")
        buffer = self.buffer
        start_offset = buffer.get_end_iter().get_offset()
        buffer.insert(buffer.get_end_iter(), prefix)
        prefix_start = buffer.get_iter_at_offset(start_offset)
        prefix_end = buffer.get_iter_at_offset(start_offset + len(prefix))
        buffer.apply_tag_by_name("time", prefix_start, prefix_end)
        text = message.rstrip()
        buffer.insert(prefix_end, text + "\n")
        text_start = buffer.get_iter_at_offset(start_offset + len(prefix))
        text_end = buffer.get_end_iter()
        if buffer.get_tag_table().lookup(tag_name):
            buffer.apply_tag_by_name(tag_name, text_start, text_end)
        if buffer.get_line_count() > self.max_lines:
            cut = buffer.get_iter_at_line(buffer.get_line_count() - self.max_lines)
            buffer.delete(buffer.get_start_iter(), cut)
        GLib.idle_add(self._scroll_to_end)

    def _scroll_to_end(self) -> bool:
        adjustment = self.text_view.get_vadjustment()
        if adjustment is not None:
            adjustment.set_value(max(0.0, adjustment.get_upper() - adjustment.get_page_size()))
        return False

    def clear(self) -> None:
        self.buffer.set_text("")


def add_class(widget: Gtk.Widget, css_class: str) -> Gtk.Widget:
    widget.add_css_class(css_class)
    return widget


def icon_button(icon_name: str, tooltip: str) -> Gtk.Button:
    button = Gtk.Button(icon_name=icon_name)
    button.set_tooltip_text(tooltip)
    button.add_css_class("flat")
    return button
