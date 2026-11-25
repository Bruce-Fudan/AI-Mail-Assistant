"""GTK4/libadwaita application bootstrap."""

from __future__ import annotations

import logging
import logging.handlers
import sys

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from .. import __app_name__, __version__
from ..config import SettingsStore
from ..util import AppPaths
from .window import MainWindow

logger = logging.getLogger("aimail")

CSS = """
.hero-card {
  background-color: @card_bg_color;
  border-radius: 18px;
  padding: 20px;
}
.hero-title {
  font-size: 24px;
  font-weight: 700;
}
.hero-icon {
  color: @accent_color;
}
.status-dot {
  font-size: 15px;
}
.dot-idle { color: alpha(@window_fg_color, 0.35); }
.dot-running { color: @success_color; }
.dot-stopping { color: @warning_color; }
.dot-error { color: @error_color; }
.status-pill {
  border-radius: 999px;
  padding: 2px 10px;
  font-size: 0.82em;
  font-weight: 600;
}
.pill-idle {
  background-color: alpha(@window_fg_color, 0.10);
  color: alpha(@window_fg_color, 0.70);
}
.pill-running {
  background-color: alpha(@success_color, 0.18);
  color: @success_color;
}
.pill-stopping {
  background-color: alpha(@warning_color, 0.18);
  color: @warning_color;
}
.pill-error {
  background-color: alpha(@error_color, 0.18);
  color: @error_color;
}
.stat-card {
  background-color: @card_bg_color;
  border-radius: 14px;
  padding: 16px;
}
.stat-value {
  font-size: 28px;
  font-weight: 700;
}
.stat-caption {
  color: alpha(@window_fg_color, 0.62);
  font-size: 0.92em;
}
.section-title {
  font-size: 1.15em;
  font-weight: 700;
}
.pill-button {
  padding: 8px 18px;
  border-radius: 999px;
}
.log-frame {
  background-color: @card_bg_color;
  border-radius: 12px;
  border: none;
}
textview.log-view,
textview.log-view text {
  background: transparent;
  font-family: monospace;
  font-size: 10pt;
}
"""


def setup_logging(paths: AppPaths) -> None:
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    if logger.handlers:
        return
    try:
        handler: logging.Handler = logging.handlers.RotatingFileHandler(
            paths.log, maxBytes=1_000_000, backupCount=2, encoding="utf-8"
        )
    except OSError:
        handler = logging.StreamHandler()
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    logger.addHandler(handler)


def _load_css() -> None:
    display = Gdk.Display.get_default()
    if display is None:
        return
    provider = Gtk.CssProvider()
    try:
        provider.load_from_data(CSS.encode("utf-8"))
    except Exception as exc:  # CSS errors should never prevent startup
        logger.warning("加载样式失败：%s", exc)
        return
    Gtk.StyleContext.add_provider_for_display(
        display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )


class AIMailApplication(Adw.Application):
    def __init__(self, paths: AppPaths):
        super().__init__(
            application_id="io.github.aimail.Assistant",
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )
        self.paths = paths
        self.store = SettingsStore(paths)
        self.config = self.store.load()
        self.startup_warnings = list(self.store.warnings)
        self.window: MainWindow | None = None

    # ------------------------------------------------------------- lifecycle
    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        GLib.set_application_name(__app_name__)
        _load_css()
        self._setup_actions()
        logger.info("%s %s 启动，数据目录：%s", __app_name__, __version__, self.paths.home)

    def do_activate(self) -> None:
        if self.window is None:
            self.window = MainWindow(
                self,
                self.paths,
                self.store,
                self.config,
                self.startup_warnings,
            )
        self.window.present()

    # --------------------------------------------------------------- actions
    def _setup_actions(self) -> None:
        def add(name: str, callback, accels: list[str] | None = None) -> None:
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _action, _param: callback())
            self.add_action(action)
            if accels:
                self.set_accels_for_action(f"app.{name}", accels)

        add("save", self._save, ["<Control>s"])
        add("check-now", self._check_now, ["F5"])
        add("open-data-dir", self._open_data_dir)
        add("about", self._about)
        add("quit", self._quit, ["<Control>q"])

    def _save(self) -> None:
        if self.window is not None:
            self.window.save_settings()

    def _check_now(self) -> None:
        if self.window is not None:
            self.window.check_now()

    def _open_data_dir(self) -> None:
        if self.window is not None:
            self.window.open_data_dir()

    def _quit(self) -> None:
        if self.window is not None:
            self.window.close()
        else:
            self.quit()

    def _about(self) -> None:
        about = Adw.AboutDialog(
            application_name=__app_name__,
            application_icon="mail-unread-symbolic",
            developer_name="AI Mail Assistant",
            version=__version__,
            comments=(
                "一个本地运行的 AI 邮件助手：收到包含指定关键词的邮件后，"
                "自动回复“收到”，再把 AI 生成的回答发送回去。\n\n"
                "所有邮箱凭据和 AI API Key 都经过加密后保存在本机。"
            ),
            license_type=Gtk.License.MIT_X11,
        )
        about.add_credit_section(
            "技术栈",
            ["Python 3", "GTK 4 / libadwaita", "cryptography (Fernet)", "requests"],
        )
        about.present(self.window)


def run(argv: list[str] | None = None, home: str | None = None) -> int:
    paths = AppPaths.resolve(home)
    try:
        paths.ensure()
    except OSError as exc:
        print(f"无法创建数据目录 {paths.home}：{exc}", file=sys.stderr)
        return 1
    setup_logging(paths)
    app = AIMailApplication(paths)
    return int(app.run(argv if argv is not None else sys.argv))
