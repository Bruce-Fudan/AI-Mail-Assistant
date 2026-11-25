"""Application window: page wiring, monitor lifecycle and event pump."""

from __future__ import annotations

import logging
import queue
from datetime import datetime
from typing import Callable

from gi.repository import Adw, Gio, GLib, Gtk

from ..config import AppConfig, ConfigError, SettingsStore
from ..monitor import MailMonitor, MonitorEvent
from ..util import AppPaths
from .settings_page import SettingsPage
from .status_page import StatusPage

logger = logging.getLogger("aimail.ui")


class MainWindow(Adw.ApplicationWindow):
    def __init__(
        self,
        application: Adw.Application,
        paths: AppPaths,
        store: SettingsStore,
        config: AppConfig,
        warnings: list[str],
    ):
        super().__init__(application=application, title="AI 邮件助手")
        self.paths = paths
        self.store = store
        self.config = config
        self._warnings = list(warnings)
        self._events: queue.Queue[tuple[int, MonitorEvent]] = queue.Queue()
        self._generation = 0
        self.monitor: MailMonitor | None = None
        self._after_stop: Callable[[], None] | None = None
        self._allow_close = False
        self._drain_source: int | None = None
        self._tick_source: int | None = None

        self.set_default_size(1120, 840)
        self.set_size_request(900, 640)

        self._build_ui()
        self._drain_source = GLib.timeout_add(120, self._drain_events)
        self._tick_source = GLib.timeout_add(1000, self._periodic)
        self.connect("close-request", self._on_close_request)
        self.connect("destroy", self._on_destroy)

        self._log_startup()
        self._sync_banner()
        if not self.config.is_configured():
            self.show_page("settings")

    # ------------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        self.header = Adw.HeaderBar()
        self.view_stack = Adw.ViewStack()
        self.view_stack.set_vexpand(True)

        self.status_page = StatusPage(
            self.config,
            on_toggle=self.toggle_monitor,
            on_check=self.check_now,
            on_open_settings=lambda: self.show_page("settings"),
            log_path=str(self.paths.log),
        )
        self.settings_page = SettingsPage(
            self.config,
            self.paths,
            on_reset=self.reset_config,
            on_open_data_dir=self.open_data_dir,
            show_toast=self.toast,
        )
        page = self.view_stack.add_titled(self.status_page, "monitor", "运行状态")
        page.set_icon_name("mail-unread-symbolic")
        page = self.view_stack.add_titled(self.settings_page, "settings", "设置")
        page.set_icon_name("emblem-system-symbolic")

        switcher = Adw.ViewSwitcherTitle()
        switcher.set_stack(self.view_stack)
        switcher.set_title("AI 邮件助手")
        self.header.set_title_widget(switcher)

        menu = Gio.Menu()
        menu.append("保存设置", "app.save")
        menu.append("打开数据目录", "app.open-data-dir")
        menu.append("关于 AI 邮件助手", "app.about")
        menu.append("退出", "app.quit")
        menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic")
        menu_button.set_tooltip_text("主菜单")
        menu_button.set_popover(Gtk.PopoverMenu.new_from_model(menu))
        self.header.pack_end(menu_button)

        self.save_button = Gtk.Button(label="保存设置")
        self.save_button.add_css_class("suggested-action")
        self.save_button.set_tooltip_text("保存设置 (Ctrl+S)")
        self.save_button.connect("clicked", lambda _button: self.save_settings())
        self.header.pack_end(self.save_button)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(self.header)
        toolbar.set_content(self.view_stack)

        self.toast_overlay = Adw.ToastOverlay()
        self.toast_overlay.set_child(toolbar)
        self.set_content(self.toast_overlay)

        self.save_button.set_sensitive(True)

    def show_page(self, name: str) -> None:
        self.view_stack.set_visible_child_name(name)

    def toast(self, message: str) -> None:
        toast = Adw.Toast.new(message)
        toast.set_timeout(3)
        self.toast_overlay.add_toast(toast)

    # --------------------------------------------------------------- startup
    def _log_startup(self) -> None:
        self.status_page.append_log("AI 邮件助手已启动", "info")
        if self._warnings:
            for warning in self._warnings:
                self.status_page.append_log(warning, "warning")
        if not self.config.is_configured():
            self.status_page.append_log("配置尚未完成，请先在“设置”中填写邮箱和 AI 接口。", "warning")
        else:
            self.status_page.append_log(
                f"已加载配置：{self.config.account_email}，关键词："
                f"{'、'.join(self.config.rule.keywords)}",
                "info",
            )

    def _sync_banner(self) -> None:
        if self._warnings:
            title = "；".join(self._warnings)
            self.status_page.set_banner(title, True, "去设置")
            self.settings_page.set_banner(title, True)
        elif not self.config.is_configured():
            self.status_page.set_banner(
                "欢迎使用 AI 邮件助手！请先完成邮箱与 AI 配置，然后开始监控。",
                True,
                "去设置",
            )
            self.settings_page.set_banner(
                "请填写以下配置，完成后点击右上角“保存设置”或直接开始监控。", True
            )
        else:
            self.status_page.set_banner("", False)
            self.settings_page.set_banner("", False)

    # ----------------------------------------------------------- settings ops
    def save_settings(self) -> bool:
        try:
            config = self.settings_page.collect_config()
        except Exception as exc:
            self._error_dialog("无法读取配置", str(exc))
            return False
        invalid = config.invalid_fields()
        if invalid:
            # Never persist an incomplete form: an empty password field would
            # otherwise overwrite the ciphertext saved earlier.
            self.settings_page.set_banner(
                "还有 %d 项配置未完成，请补全后再保存：%s"
                % (len(invalid), "、".join(invalid[:4]) + ("…" if len(invalid) > 4 else "")),
                True,
            )
            self.status_page.set_banner("配置尚未完成，请补全后再保存。", True, "去设置")
            self.show_page("settings")
            self.toast("未保存：还有 %d 项配置未完成" % len(invalid))
            return False

        changed = config != self.config
        try:
            self.store.save(config)
        except (ConfigError, OSError) as exc:
            self._error_dialog("保存失败", str(exc))
            return False

        self.config = config
        self._warnings = []
        self.status_page.set_config(config)
        self.settings_page.set_banner("", False)
        self._sync_banner()

        if self._after_stop is not None:
            # The previous session is still shutting down; apply the newest
            # config when it finishes instead of starting twice.
            self._after_stop = lambda: self._start_with_config(config)
            self.toast("设置已保存，正在应用…")
            return True

        was_running = self.monitor is not None and self.monitor.is_alive()
        if was_running and self.monitor is not None and self.monitor.is_stopping():
            # The user already asked to stop; don't silently restart it.
            self.toast("设置已保存，监控正在停止")
            return True
        if was_running:
            if not changed:
                self.toast("设置未变化")
            else:
                self.toast("设置已保存，正在重启监控…")
                self._stop_monitor_then(lambda: self._start_with_config(config))
        else:
            self.toast("设置已保存；可到“运行状态”开始监控")
        logger.info("设置已保存")
        return True

    def reset_config(self) -> None:
        if self.monitor is not None:
            self.monitor.stop(wait=True, timeout=3.0)
            if self.monitor.is_alive():
                # An SMTP/AI operation is still in flight. Clearing settings
                # now would let that operation send a reply after the reset.
                self.toast("上一封邮件仍在处理中，请稍后再试清空")
                return
            self.monitor = None
        self._after_stop = None
        self._generation += 1
        try:
            self.store.reset()
        except ConfigError as exc:
            self._error_dialog("清空失败", str(exc))
            return
        self.config = AppConfig()
        self._warnings = []
        self.settings_page.load_config(self.config)
        self.settings_page.set_banner("", False)
        self.status_page.set_config(self.config)
        self.status_page.set_stats({})
        self.status_page.set_state("idle")
        self.status_page.log_view.clear()
        self.status_page.append_log("配置已清空，请重新填写。", "warning")
        self._sync_banner()
        self.show_page("settings")
        self.toast("配置已清空")

    # ---------------------------------------------------------- monitor flow
    def toggle_monitor(self) -> None:
        if self._after_stop is not None:
            self.toast("正在应用设置，请稍候…")
            return
        monitor = self.monitor
        if monitor is not None and monitor.is_alive():
            if monitor.is_stopping():
                self.toast("监控正在停止，请稍候…")
                return
            self.status_page.set_state("stopping", "正在停止监控…")
            self.status_page.append_log("正在停止监控…", "info")
            monitor.stop()
            return

        try:
            config = self.settings_page.collect_config()
        except Exception as exc:
            self._error_dialog("无法读取配置", str(exc))
            return
        invalid = config.invalid_fields()
        if invalid:
            # Do not persist an incomplete form here: an empty password field
            # would otherwise overwrite the already saved credential.
            self.settings_page.set_banner(
                "开始监控前还需要补全 %d 项配置：%s"
                % (len(invalid), "、".join(invalid)),
                True,
            )
            self.show_page("settings")
            self._show_missing_dialog(invalid)
            return
        try:
            self.store.save(config)
        except (ConfigError, OSError) as exc:
            self._error_dialog("保存失败", str(exc))
            return
        self.config = config
        self._warnings = []
        self.status_page.set_config(config)
        self._sync_banner()
        self.monitor = None
        self._start_with_config(config)

    def _start_with_config(self, config: AppConfig) -> None:
        if self.monitor is not None and self.monitor.is_alive():
            return
        invalid = config.invalid_fields()
        if invalid:
            self._show_missing_dialog(invalid)
            return
        self._generation += 1
        generation = self._generation
        monitor = MailMonitor(
            config,
            self.paths,
            event_sink=lambda event, g=generation: self._enqueue(g, event),
        )
        self.monitor = monitor
        self.status_page.set_state("starting")
        self.status_page.set_stats({})
        self.status_page.append_log(
            f"正在启动监控（每 {config.rule.poll_seconds} 秒检查一次）…", "info"
        )
        if not monitor.start():
            self.status_page.set_state("error", "无法启动监控线程")
            self.toast("无法启动监控，请重试")
            self.monitor = None

    def _stop_monitor_then(self, action: Callable[[], None] | None) -> None:
        monitor = self.monitor
        if monitor is None or not monitor.is_alive():
            self.monitor = None
            if action is not None:
                action()
            elif self.status_page.get_state() != "idle":
                self.status_page.set_state("idle")
            return
        self._after_stop = action
        self.status_page.set_state("stopping", "正在应用设置…")
        monitor.stop()

    def check_now(self) -> None:
        if self.monitor is not None and self.monitor.check_now():
            self.status_page.append_log("已请求立即检查", "info")
            self.toast("正在检查新邮件…")
        else:
            self.toast("监控未运行")

    def _enqueue(self, generation: int, event: MonitorEvent) -> None:
        self._events.put((generation, event))

    def _drain_events(self) -> bool:
        while True:
            try:
                generation, event = self._events.get_nowait()
            except queue.Empty:
                return True
            if generation != self._generation:
                continue
            if event.kind == "status":
                state = event.data.get("state", "")
                detail = event.data.get("detail", "")
                if state == "stopped" and self._after_stop is not None:
                    # Keep the "stopping" UI until the pending action runs.
                    continue
                self.status_page.set_state(state, detail)
            elif event.kind == "log":
                if event.level == "debug":
                    continue
                self.status_page.append_log(event.message, event.level)
            elif event.kind == "stats":
                self.status_page.set_stats(event.data)
            elif event.kind == "check":
                self.status_page.set_last_check(
                    bool(event.data.get("ok")),
                    event.data.get("detail", ""),
                    event.data.get("time", datetime.now().astimezone().isoformat()),
                )

    def _periodic(self) -> bool:
        if self._after_stop is not None:
            monitor = self.monitor
            if monitor is None or not monitor.is_alive():
                action = self._after_stop
                self._after_stop = None
                self.monitor = None
                if action is not None:
                    GLib.idle_add(self._run_after_stop, action)
                else:
                    self.status_page.set_state("idle")
            return True
        monitor = self.monitor
        if monitor is not None and not monitor.is_alive() and not monitor.is_stopping():
            self.monitor = None
            if self.status_page.get_state() in {"running", "stopping"}:
                self.status_page.set_state("idle", "监控线程已退出")
        return True

    @staticmethod
    def _run_after_stop(action: Callable[[], None]) -> bool:
        action()
        return False

    # ---------------------------------------------------------------- dialogs
    def _show_missing_dialog(self, missing: list[str]) -> None:
        dialog = Adw.AlertDialog(
            heading="配置尚未完成",
            body="请补全以下配置后再开始监控：\n\n" + "\n".join(f"• {item}" for item in missing),
        )
        dialog.add_response("close", "知道了")
        dialog.add_response("settings", "去设置")
        dialog.set_default_response("settings")
        dialog.set_close_response("close")

        def on_response(_dialog, response: str) -> None:
            if response == "settings":
                self.show_page("settings")

        dialog.connect("response", on_response)
        dialog.present(self)

    def _error_dialog(self, heading: str, body: str) -> None:
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("close", "知道了")
        dialog.set_close_response("close")
        dialog.present(self)

    def open_data_dir(self) -> None:
        try:
            Gio.AppInfo.launch_default_for_uri(self.paths.home.as_uri(), None)
        except Exception as exc:
            self.toast(f"无法打开数据目录：{exc}")

    # ---------------------------------------------------------------- closing
    def _on_close_request(self, _window) -> bool:
        if self._allow_close:
            return False
        if self.monitor is not None and self.monitor.is_alive():
            dialog = Adw.AlertDialog(
                heading="监控正在运行",
                body="退出应用会停止监控。确定要退出吗？",
            )
            dialog.add_response("cancel", "取消")
            dialog.add_response("quit", "退出")
            dialog.set_response_appearance("quit", Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_default_response("cancel")
            dialog.set_close_response("cancel")

            def on_response(_dialog, response: str) -> None:
                if response == "quit":
                    self._allow_close = True
                    if self.monitor is not None:
                        self.monitor.stop()
                    self.destroy()

            dialog.connect("response", on_response)
            dialog.present(self)
            return True
        return False

    def _on_destroy(self, _window) -> None:
        for source in (self._drain_source, self._tick_source):
            if source is not None:
                try:
                    GLib.source_remove(source)
                except Exception:
                    pass
        self._drain_source = None
        self._tick_source = None
        if self.monitor is not None:
            self.monitor.stop()
