"""The dashboard page: monitor state, stats and activity log."""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from gi.repository import Adw, GLib, Gtk, Pango

from ..config import AppConfig, join_keywords
from ..util import human_relative_time
from .widgets import LogView, StatCard, StatusDot, StatusPill


class StatusPage(Gtk.Box):
    def __init__(
        self,
        config: AppConfig,
        on_toggle: Callable[[], None],
        on_check: Callable[[], None],
        on_open_settings: Callable[[], None],
        log_path: str = "",
    ):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._on_toggle = on_toggle
        self._on_check = on_check
        self._on_open_settings = on_open_settings
        self._state = "idle"
        self._last_check_dt: datetime | None = None
        self._last_check_ok: bool | None = None
        self._last_check_detail = ""

        self.banner = Adw.Banner(title="", revealed=False)
        self.banner.set_button_label("去设置")
        self.banner.connect("button-clicked", lambda _banner: self._on_open_settings())
        self.banner.set_visible(False)
        self.append(self.banner)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)
        clamp = Adw.Clamp(maximum_size=900, tightening_threshold=700)
        clamp.set_margin_top(24)
        clamp.set_margin_bottom(28)
        clamp.set_margin_start(24)
        clamp.set_margin_end(24)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
        clamp.set_child(outer)
        scrolled.set_child(clamp)
        self.append(scrolled)

        outer.append(self._build_hero())
        outer.append(self._build_stats())
        outer.append(self._build_log_section(log_path))
        self.set_config(config)

        self._tick_source = GLib.timeout_add(1000, self._tick)
        self.connect("destroy", self._on_destroy)

    # ------------------------------------------------------------------ UI
    def _build_hero(self) -> Gtk.Widget:
        card = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=18)
        card.add_css_class("hero-card")

        icon = Gtk.Image.new_from_icon_name("mail-unread-symbolic")
        icon.set_pixel_size(46)
        icon.set_valign(Gtk.Align.START)
        icon.add_css_class("hero-icon")
        card.append(icon)

        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        text_box.set_hexpand(True)
        text_box.set_valign(Gtk.Align.CENTER)

        title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.dot = StatusDot()
        self.pill = StatusPill("已停止")
        self.title_label = Gtk.Label(label="未在监控", xalign=0)
        self.title_label.add_css_class("hero-title")
        title_row.append(self.dot)
        title_row.append(self.title_label)
        title_row.append(self.pill)
        text_box.append(title_row)

        self.subtitle_label = Gtk.Label(label="配置邮箱和 AI 接口后开始监控", xalign=0)
        self.subtitle_label.add_css_class("dim-label")
        self.subtitle_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.subtitle_label.set_selectable(False)
        text_box.append(self.subtitle_label)

        self.check_label = Gtk.Label(label="尚未检查", xalign=0)
        self.check_label.add_css_class("caption")
        self.check_label.add_css_class("dim-label")
        text_box.append(self.check_label)
        card.append(text_box)

        buttons = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        buttons.set_valign(Gtk.Align.CENTER)
        self.start_button = Gtk.Button(label="开始监控")
        self.start_button.add_css_class("suggested-action")
        self.start_button.add_css_class("pill-button")
        self.start_button.connect("clicked", lambda _button: self._on_toggle())
        self.check_button = Gtk.Button(label="立即检查")
        self.check_button.add_css_class("flat")
        self.check_button.set_sensitive(False)
        self.check_button.connect("clicked", lambda _button: self._on_check())
        buttons.append(self.start_button)
        buttons.append(self.check_button)
        card.append(buttons)
        return card

    def _build_stats(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.set_homogeneous(True)
        self.stat_matched = StatCard("匹配邮件")
        self.stat_acked = StatCard("已发送确认")
        self.stat_replied = StatCard("已发送 AI 回复")
        self.stat_errors = StatCard("错误")
        for card in (self.stat_matched, self.stat_acked, self.stat_replied, self.stat_errors):
            box.append(card)
        return box

    def _build_log_section(self, log_path: str) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title = Gtk.Label(label="活动日志", xalign=0)
        title.add_css_class("section-title")
        title.set_hexpand(True)
        self.clear_button = Gtk.Button(label="清空")
        self.clear_button.add_css_class("flat")
        self.clear_button.set_tooltip_text("仅清空界面日志，不会删除日志文件")
        self.clear_button.connect("clicked", lambda _button: self.log_view.clear())
        header.append(title)
        header.append(self.clear_button)
        box.append(header)

        self.log_view = LogView()
        self.log_view.set_vexpand(True)
        box.append(self.log_view)

        self.log_path_label: Gtk.Label | None = None
        if log_path:
            path_label = Gtk.Label(label=f"日志文件：{log_path}", xalign=0)
            path_label.add_css_class("caption")
            path_label.add_css_class("dim-label")
            path_label.set_selectable(True)
            path_label.set_ellipsize(Pango.EllipsizeMode.END)
            box.append(path_label)
            self.log_path_label = path_label
        return box

    # -------------------------------------------------------------- updates
    def set_config(self, config: AppConfig) -> None:
        self._config = config
        keywords = join_keywords(config.rule.keywords) or "（未设置）"
        if config.account_email:
            subtitle = (
                f"{config.account_email} · 关键词：{keywords} · "
                f"每 {config.rule.poll_seconds} 秒检查"
            )
        else:
            subtitle = "尚未配置邮箱，请先完成设置"
        self.subtitle_label.set_text(subtitle)
        self.subtitle_label.set_tooltip_text(subtitle)

    def set_banner(self, title: str, visible: bool, button_label: str = "去设置") -> None:
        self.banner.set_title(title)
        self.banner.set_button_label(button_label)
        if visible:
            self.banner.set_visible(True)
            self.banner.set_revealed(True)
        else:
            self.banner.set_revealed(False)
            self.banner.set_visible(False)

    def _set_start_style(self, style: str) -> None:
        for css_class in ("suggested-action", "destructive-action", "flat"):
            self.start_button.remove_css_class(css_class)
        if style:
            self.start_button.add_css_class(style)

    def set_state(self, state: str, detail: str = "") -> None:
        if state == "stopped":
            state = "idle"
        self._state = state
        if state == "running":
            self.dot.set_state("running")
            self.pill.set_state("running", "监控中")
            self.title_label.set_text("正在监控收件箱")
            self.start_button.set_label("停止监控")
            self.start_button.set_sensitive(True)
            self._set_start_style("destructive-action")
            self.check_button.set_sensitive(True)
        elif state == "stopping":
            self.dot.set_state("stopping")
            self.pill.set_state("stopping", "正在停止")
            self.title_label.set_text("正在停止监控…")
            self.start_button.set_label("正在停止…")
            self.start_button.set_sensitive(False)
            self._set_start_style("")
            self.check_button.set_sensitive(False)
        elif state == "starting":
            self.dot.set_state("stopping")
            self.pill.set_state("stopping", "正在启动")
            self.title_label.set_text("正在启动监控…")
            self.start_button.set_label("正在启动…")
            self.start_button.set_sensitive(False)
            self._set_start_style("")
            self.check_button.set_sensitive(False)
        elif state == "error":
            self.dot.set_state("error")
            self.pill.set_state("error", "需要处理")
            self.title_label.set_text("监控未运行")
            self.start_button.set_label("开始监控")
            self.start_button.set_sensitive(True)
            self._set_start_style("suggested-action")
            self.check_button.set_sensitive(False)
        else:
            self.dot.set_state("idle")
            self.pill.set_state("idle", "已停止")
            self.title_label.set_text("未在监控")
            self.start_button.set_label("开始监控")
            self.start_button.set_sensitive(True)
            self._set_start_style("suggested-action")
            self.check_button.set_sensitive(False)
        if detail:
            self.pill.set_tooltip_text(detail)
            if state == "error":
                self.check_label.set_text(detail)

    def get_state(self) -> str:
        return self._state

    def set_stats(self, stats: dict) -> None:
        self.stat_matched.set_value(str(stats.get("matched", 0)))
        self.stat_acked.set_value(str(stats.get("acked", 0)))
        self.stat_replied.set_value(str(stats.get("replied", 0)))
        self.stat_errors.set_value(str(stats.get("errors", 0)))

    def set_last_check(self, ok: bool, detail: str, stamp: str) -> None:
        self._last_check_ok = ok
        self._last_check_detail = detail or ""
        try:
            parsed = datetime.fromisoformat(stamp)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
            self._last_check_dt = parsed
        except ValueError:
            self._last_check_dt = datetime.now().astimezone()
        self._refresh_check_label()
        self.check_label.set_tooltip_text(self._last_check_detail)

    def _refresh_check_label(self) -> None:
        if self._last_check_dt is None:
            self.check_label.set_text("尚未检查")
            return
        now = datetime.now().astimezone()
        delta = (now - self._last_check_dt).total_seconds()
        status = "正常" if self._last_check_ok else "失败"
        if not self._last_check_ok:
            status = "失败"
        self.check_label.set_text(
            f"上次检查：{self._last_check_dt.strftime('%H:%M:%S')} · "
            f"{status}（{human_relative_time(delta)}）"
        )

    def _tick(self) -> bool:
        self._refresh_check_label()
        return True

    def _on_destroy(self, _widget) -> None:
        if self._tick_source is not None:
            try:
                GLib.source_remove(self._tick_source)
            except Exception:
                pass
            self._tick_source = None

    def append_log(self, message: str, level: str = "info") -> None:
        self.log_view.append(message, level)
