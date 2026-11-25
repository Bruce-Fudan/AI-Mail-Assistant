"""The configuration page."""

from __future__ import annotations

from typing import Callable

from gi.repository import Adw, Gtk

from ..ai_client import AIClient, build_endpoint, normalize_base_url
from ..config import AppConfig, split_addresses, split_keywords
from ..mailer import SmtpSender, test_imap
from ..util import AppPaths
from .widgets import run_async

PROVIDERS = {
    "gmail.com": ("imap.gmail.com", 993, "ssl", "smtp.gmail.com", 465, "ssl", "Gmail"),
    "googlemail.com": ("imap.gmail.com", 993, "ssl", "smtp.gmail.com", 465, "ssl", "Gmail"),
    "qq.com": ("imap.qq.com", 993, "ssl", "smtp.qq.com", 465, "ssl", "QQ 邮箱"),
    "foxmail.com": ("imap.qq.com", 993, "ssl", "smtp.qq.com", 465, "ssl", "Foxmail"),
    "163.com": ("imap.163.com", 993, "ssl", "smtp.163.com", 465, "ssl", "163 邮箱"),
    "126.com": ("imap.126.com", 993, "ssl", "smtp.126.com", 465, "ssl", "126 邮箱"),
    "yeah.net": ("imap.yeah.net", 993, "ssl", "smtp.yeah.net", 465, "ssl", "Yeah 邮箱"),
    "outlook.com": (
        "outlook.office365.com",
        993,
        "ssl",
        "smtp.office365.com",
        587,
        "starttls",
        "Outlook",
    ),
    "hotmail.com": (
        "outlook.office365.com",
        993,
        "ssl",
        "smtp.office365.com",
        587,
        "starttls",
        "Outlook",
    ),
    "live.com": (
        "outlook.office365.com",
        993,
        "ssl",
        "smtp.office365.com",
        587,
        "starttls",
        "Outlook",
    ),
    "yahoo.com": ("imap.mail.yahoo.com", 993, "ssl", "smtp.mail.yahoo.com", 465, "ssl", "Yahoo"),
    "icloud.com": ("imap.mail.me.com", 993, "ssl", "smtp.mail.me.com", 587, "starttls", "iCloud"),
    "me.com": ("imap.mail.me.com", 993, "ssl", "smtp.mail.me.com", 587, "starttls", "iCloud"),
    "sina.com": ("imap.sina.com", 993, "ssl", "smtp.sina.com", 465, "ssl", "新浪邮箱"),
    "139.com": ("imap.139.com", 993, "ssl", "smtp.139.com", 465, "ssl", "139 邮箱"),
}
SECURITY_ORDER = ["ssl", "starttls", "none"]
SECURITY_LABELS = ["SSL/TLS（推荐）", "STARTTLS", "不加密（仅本机）"]
SCOPE_VALUES = [(True, True), (True, False), (False, True)]


def _short(text: str, limit: int = 160) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _int(text: str, default: int) -> int:
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return default


def _float(text: str, default: float) -> float:
    try:
        return float(str(text).strip())
    except (TypeError, ValueError):
        return default


class PromptDialog(Adw.Dialog):
    def __init__(self, initial: str, on_save: Callable[[str], None]):
        super().__init__()
        self.set_title("系统提示词")
        self.set_content_width(720)
        self.set_content_height(520)
        self._on_save = on_save

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        cancel = Gtk.Button(label="取消")
        cancel.connect("clicked", lambda _button: self.close())
        save = Gtk.Button(label="保存")
        save.add_css_class("suggested-action")
        save.connect("clicked", self._save)
        header.pack_start(cancel)
        header.pack_end(save)
        toolbar.add_top_bar(header)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_top(12)
        box.set_margin_bottom(18)
        box.set_margin_start(18)
        box.set_margin_end(18)
        hint = Gtk.Label(
            label="这段内容会原样作为 system 消息发送给 AI；程序不会添加、修改或补全任何提示词。留空则不发送 system 消息。",
            xalign=0,
        )
        hint.add_css_class("dim-label")
        hint.set_wrap(True)
        box.append(hint)
        frame = Gtk.Frame()
        frame.add_css_class("log-frame")
        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        self.text_view = Gtk.TextView()
        self.text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.text_view.set_top_margin(12)
        self.text_view.set_bottom_margin(12)
        self.text_view.set_left_margin(12)
        self.text_view.set_right_margin(12)
        self.text_view.get_buffer().set_text(initial or "")
        scroller.set_child(self.text_view)
        frame.set_child(scroller)
        box.append(frame)
        toolbar.set_content(box)
        self.set_child(toolbar)

    def _save(self, _button) -> None:
        buffer = self.text_view.get_buffer()
        text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
        try:
            self._on_save(text)
        finally:
            self.close()


class SettingsPage(Gtk.Box):
    def __init__(
        self,
        config: AppConfig,
        paths: AppPaths,
        *,
        on_reset: Callable[[], None],
        on_open_data_dir: Callable[[], None],
        show_toast: Callable[[str], None],
    ):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.paths = paths
        self._on_reset = on_reset
        self._on_open_data_dir = on_open_data_dir
        self._show_toast = show_toast
        self._system_prompt = ""
        self._prompt_dialog: PromptDialog | None = None

        self.banner = Adw.Banner(title="", revealed=False)
        self.banner.set_button_label("知道了")
        self.banner.connect("button-clicked", lambda _banner: self.banner.set_visible(False))
        self.banner.set_visible(False)
        self.append(self.banner)

        self.page = Adw.PreferencesPage()
        self.page.set_vexpand(True)
        self.append(self.page)

        self._build_account_group()
        self._build_imap_group()
        self._build_smtp_group()
        self._build_rule_group()
        self._build_ai_group()
        self._build_behavior_group()
        self._build_data_group()

        self.load_config(config)

    # ------------------------------------------------------------- builders
    @staticmethod
    def _entry(title: str = "") -> Adw.EntryRow:
        return Adw.EntryRow(title=title)

    @staticmethod
    def _password(title: str = "") -> Adw.PasswordEntryRow:
        return Adw.PasswordEntryRow(title=title)

    @staticmethod
    def _flat_button(label: str) -> Gtk.Button:
        button = Gtk.Button(label=label)
        button.add_css_class("flat")
        button.set_valign(Gtk.Align.CENTER)
        return button

    @staticmethod
    def _test_row(title: str, button_label: str) -> tuple[Adw.ActionRow, Gtk.Button]:
        row = Adw.ActionRow(title=title, subtitle="尚未测试")
        row.set_subtitle_selectable(True)
        button = Gtk.Button(label=button_label)
        button.add_css_class("flat")
        button.set_valign(Gtk.Align.CENTER)
        row.add_suffix(button)
        return row, button

    def _combo(self, title: str, labels: list[str], selected: int = 0) -> Adw.ComboRow:
        row = Adw.ComboRow(title=title)
        row.set_model(Gtk.StringList.new(labels))
        row.set_selected(max(0, min(selected, len(labels) - 1)))
        return row

    def _spin(
        self, title: str, minimum: float, maximum: float, step: float, value: float, digits: int = 0
    ) -> Adw.SpinRow:
        row = Adw.SpinRow.new_with_range(minimum, maximum, step)
        row.set_title(title)
        row.set_digits(digits)
        row.set_value(value)
        return row

    def _build_account_group(self) -> None:
        group = Adw.PreferencesGroup(title="邮箱账户")
        group.set_description("登录邮箱的地址与发件人名称。密码只保存在本机，并经过加密。")

        self.email_entry = self._entry("邮箱地址")
        group.add(self.email_entry)

        self.name_entry = self._entry("发件人名称")
        group.add(self.name_entry)

        autofill = Adw.ActionRow(
            title="常用邮箱自动填写",
            subtitle="根据邮箱域名填写 IMAP/SMTP 服务器和端口",
        )
        autofill_button = self._flat_button("自动填写")
        autofill_button.connect("clicked", lambda _button: self._autofill_provider())
        autofill.add_suffix(autofill_button)
        group.add(autofill)

        encrypt_row = Adw.ActionRow(
            title="凭据加密",
            subtitle="IMAP/SMTP 密码与 AI API Key 使用 Fernet 加密后保存在本地",
        )
        lock = Gtk.Image.new_from_icon_name("security-high-symbolic")
        lock.set_valign(Gtk.Align.CENTER)
        encrypt_row.add_suffix(lock)
        group.add(encrypt_row)
        self.page.add(group)

    def _build_imap_group(self) -> None:
        group = Adw.PreferencesGroup(title="收件服务器（IMAP）")
        group.set_description("用于读取新邮件。Gmail、QQ、163 等需使用应用专用密码/授权码。")

        self.imap_host = self._entry("IMAP 服务器")
        group.add(self.imap_host)
        self.imap_port = self._entry("IMAP 端口")
        group.add(self.imap_port)
        self.imap_security = self._combo("加密方式", SECURITY_LABELS, 0)
        group.add(self.imap_security)
        self.imap_user = self._entry("IMAP 用户名")
        group.add(self.imap_user)
        self.imap_password = self._password("IMAP 密码 / 授权码")
        group.add(self.imap_password)
        self.imap_mailbox = self._entry("收件箱文件夹")
        group.add(self.imap_mailbox)

        self.imap_test_row, self.imap_test_button = self._test_row(
            "连接测试", "测试 IMAP 连接"
        )
        self.imap_test_button.connect("clicked", lambda _button: self._test_imap())
        group.add(self.imap_test_row)
        self.page.add(group)

    def _build_smtp_group(self) -> None:
        group = Adw.PreferencesGroup(title="发件服务器（SMTP）")
        group.set_description("用于回复“收到”和发送 AI 生成的答案；用户名留空时默认使用邮箱地址。")

        self.smtp_host = self._entry("SMTP 服务器")
        group.add(self.smtp_host)
        self.smtp_port = self._entry("SMTP 端口")
        group.add(self.smtp_port)
        self.smtp_security = self._combo("加密方式", SECURITY_LABELS, 0)
        group.add(self.smtp_security)
        self.smtp_auth = Adw.SwitchRow(
            title="需要登录认证",
            subtitle="关闭后不会发送用户名/密码（仅适用于本机或可信中继）",
        )
        self.smtp_auth.connect("notify::active", lambda _row, _pspec: self._sync_auth_sensitivity())
        group.add(self.smtp_auth)
        self.smtp_user = self._entry("SMTP 用户名")
        group.add(self.smtp_user)
        self.smtp_password = self._password("SMTP 密码 / 授权码")
        group.add(self.smtp_password)

        self.smtp_test_row, self.smtp_test_button = self._test_row(
            "连接测试", "测试 SMTP 连接"
        )
        self.smtp_test_button.connect("clicked", lambda _button: self._test_smtp())
        group.add(self.smtp_test_row)

        self.smtp_send_row, self.smtp_send_button = self._test_row(
            "发送测试邮件", "发送到我的邮箱"
        )
        self.smtp_send_row.set_subtitle("会向你的邮箱发送一封测试邮件")
        self.smtp_send_button.connect("clicked", lambda _button: self._send_test_mail())
        group.add(self.smtp_send_row)
        self.page.add(group)

    def _build_rule_group(self) -> None:
        group = Adw.PreferencesGroup(title="触发规则")
        group.set_description("只有匹配关键词的新邮件才会自动回复。")

        self.keywords_entry = self._entry("触发关键词")
        group.add(self.keywords_entry)
        keywords_hint = Adw.ActionRow(
            title="多个关键词",
            subtitle="用逗号分隔，匹配任意一个即触发；不区分大小写",
        )
        group.add(keywords_hint)

        self.scope_combo = self._combo(
            "匹配范围", ["主题或正文", "仅主题", "仅正文"], 0
        )
        group.add(self.scope_combo)

        self.poll_spin = self._spin("检查间隔（秒）", 5, 300, 5, 10)
        self.poll_spin.set_subtitle("间隔越短响应越快，但会增加邮箱服务器请求频率")
        group.add(self.poll_spin)

        self.ignore_auto = Adw.SwitchRow(
            title="忽略自动回复和批量邮件",
            subtitle="避免与其他自动回复程序形成循环（推荐开启）",
        )
        group.add(self.ignore_auto)
        self.process_existing = Adw.SwitchRow(
            title="首次启动时处理已有未读邮件",
            subtitle="默认关闭，只处理启动之后收到的新邮件",
        )
        group.add(self.process_existing)
        self.allowed_entry = self._entry("只响应这些发件人（可选）")
        group.add(self.allowed_entry)
        allowed_hint = Adw.ActionRow(
            title="发件人白名单",
            subtitle="留空表示允许所有发件人；可填完整邮箱或 @example.com 形式域名。"
            "如需给自己发测试邮件，请把本账号也加入白名单。",
        )
        group.add(allowed_hint)
        self.page.add(group)

    def _build_ai_group(self) -> None:
        group = Adw.PreferencesGroup(title="AI 接口")
        group.set_description(
            "支持 OpenAI 兼容的 Chat Completions 接口，例如 llama.cpp、vLLM、"
            "LM Studio、Ollama 的 /v1 接口。模型名称建议填写 /v1/models 返回的 id（可留空）；"
            "高级选项可忽略输出长度与超时限制。"
        )

        self.ai_base = self._entry("API 基础地址")
        self.ai_base.connect("changed", lambda _entry: self._update_endpoint_preview())
        group.add(self.ai_base)

        self.endpoint_row = Adw.ActionRow(title="实际请求地址", subtitle="—")
        self.endpoint_row.set_subtitle_selectable(True)
        group.add(self.endpoint_row)

        self.ai_model = self._entry("模型名称")
        group.add(self.ai_model)
        self.ai_key = self._password("API Key（可选）")
        group.add(self.ai_key)

        self.advanced = Adw.ExpanderRow(title="高级选项")
        self.ai_path = self._entry("接口路径")
        self.ai_path.connect("changed", lambda _entry: self._update_endpoint_preview())
        self.advanced.add_row(self.ai_path)
        self.ai_use_backend_defaults = Adw.SwitchRow(
            title="使用后端 AI 默认选项",
            subtitle="不发送温度、top_p、top_k、思考强度等采样参数，全部由后端决定",
        )
        self.ai_use_backend_defaults.connect(
            "notify::active", lambda _row, _pspec: self._sync_ai_limits()
        )
        self.advanced.add_row(self.ai_use_backend_defaults)
        self.ai_temperature = self._spin("温度（temperature）", 0.0, 2.0, 0.1, 0.7, 1)
        self.advanced.add_row(self.ai_temperature)
        self.ai_max_tokens = self._spin("最大输出 tokens", 64, 32768, 64, 1024)
        self.advanced.add_row(self.ai_max_tokens)
        self.ai_timeout = self._spin("请求超时（秒）", 10, 600, 10, 120)
        self.advanced.add_row(self.ai_timeout)

        self.ai_ignore_length = Adw.SwitchRow(
            title="忽略输出长度上限",
            subtitle="不发送 max_tokens，让模型/服务自行决定何时停止",
        )
        self.ai_ignore_length.connect(
            "notify::active", lambda _row, _pspec: self._sync_ai_limits()
        )
        self.advanced.add_row(self.ai_ignore_length)
        self.ai_ignore_timeout = Adw.SwitchRow(
            title="忽略请求超时",
            subtitle="一直等待 AI 返回；服务无响应时处理线程会一直等待",
        )
        self.ai_ignore_timeout.connect(
            "notify::active", lambda _row, _pspec: self._sync_ai_limits()
        )
        self.advanced.add_row(self.ai_ignore_timeout)
        group.add(self.advanced)

        self.prompt_row = Adw.ActionRow(
            title="系统提示词", subtitle="（空，不发送 system 消息）"
        )
        prompt_button = self._flat_button("编辑")
        prompt_button.connect("clicked", lambda _button: self._edit_prompt())
        self.prompt_row.add_suffix(prompt_button)
        group.add(self.prompt_row)

        self.ai_test_row, self.ai_test_button = self._test_row(
            "连接测试", "测试 AI 接口"
        )
        self.ai_test_button.connect("clicked", lambda _button: self._test_ai())
        group.add(self.ai_test_row)
        self.page.add(group)

    def _build_behavior_group(self) -> None:
        group = Adw.PreferencesGroup(title="自动回复")
        group.set_description("先发一封简短确认，再把 AI 生成的完整回答发给发件人。")

        self.ack_entry = self._entry("立即回复内容")
        group.add(self.ack_entry)

        self.error_notice = Adw.SwitchRow(
            title="AI 失败时通知发件人",
            subtitle="关闭时只在本应用的日志中记录错误",
        )
        group.add(self.error_notice)
        self.page.add(group)

    def _build_data_group(self) -> None:
        group = Adw.PreferencesGroup(title="数据与安全")
        data_row = Adw.ActionRow(title="数据目录", subtitle=str(self.paths.home))
        data_row.set_subtitle_selectable(True)
        self.data_dir_row = data_row
        open_button = self._flat_button("打开")
        open_button.connect("clicked", lambda _button: self._on_open_data_dir())
        data_row.add_suffix(open_button)
        group.add(data_row)

        key_row = Adw.ActionRow(
            title="加密密钥",
            subtitle=f"{self.paths.secret_key.name}（权限 600，请勿分享或删除）",
        )
        key_row.set_subtitle_selectable(True)
        group.add(key_row)

        reset_row = Adw.ActionRow(
            title="清空所有配置",
            subtitle="删除本地设置文件（不会删除邮件）。加密密钥会保留。",
        )
        reset_button = Gtk.Button(label="清空")
        reset_button.add_css_class("destructive-action")
        reset_button.set_valign(Gtk.Align.CENTER)
        reset_button.connect("clicked", lambda _button: self._confirm_reset())
        reset_row.add_suffix(reset_button)
        group.add(reset_row)
        self.page.add(group)

    # --------------------------------------------------------------- config
    def load_config(self, config: AppConfig) -> None:
        self.email_entry.set_text(config.account_email)
        self.name_entry.set_text(config.account_name)

        self.imap_host.set_text(config.imap.host)
        self.imap_port.set_text(str(config.imap.port))
        self.imap_security.set_selected(SECURITY_ORDER.index(config.imap.security))
        self.imap_user.set_text(config.imap.username)
        self.imap_password.set_text(config.imap.password)
        self.imap_mailbox.set_text(config.imap.mailbox or "INBOX")

        self.smtp_host.set_text(config.smtp.host)
        self.smtp_port.set_text(str(config.smtp.port))
        self.smtp_security.set_selected(SECURITY_ORDER.index(config.smtp.security))
        self.smtp_auth.set_active(config.smtp.use_auth)
        self.smtp_user.set_text(config.smtp.username)
        self.smtp_password.set_text(config.smtp.password)

        self.keywords_entry.set_text(
            "，".join(config.rule.keywords) if config.rule.keywords else ""
        )
        self.scope_combo.set_selected(config.rule.match_scope)
        self.poll_spin.set_value(config.rule.poll_seconds)
        self.ignore_auto.set_active(config.rule.ignore_auto_submitted)
        self.process_existing.set_active(config.rule.process_existing_unread)
        self.allowed_entry.set_text("，".join(config.rule.allowed_senders))

        self.ai_base.set_text(config.ai.base_url)
        self.ai_path.set_text(config.ai.endpoint_path)
        self.ai_model.set_text(config.ai.model)
        self.ai_key.set_text(config.ai.api_key)
        self.ai_temperature.set_value(config.ai.temperature)
        self.ai_max_tokens.set_value(config.ai.max_tokens)
        self.ai_timeout.set_value(config.ai.timeout_seconds)
        self.ai_ignore_length.set_active(config.ai.ignore_output_limit)
        self.ai_ignore_timeout.set_active(config.ai.ignore_timeout)
        self.ai_use_backend_defaults.set_active(config.ai.use_backend_defaults)
        self._system_prompt = config.ai.system_prompt
        self._update_prompt_row()

        self.ack_entry.set_text(config.behavior.ack_text)
        self.error_notice.set_active(config.behavior.send_error_notice)
        self._update_endpoint_preview()
        self._sync_auth_sensitivity()
        self._sync_ai_limits()
        self.imap_test_row.set_subtitle("尚未测试")
        self.smtp_test_row.set_subtitle("尚未测试")
        self.smtp_send_row.set_subtitle("会向你的邮箱发送一封测试邮件")
        self.ai_test_row.set_subtitle("尚未测试")

    def collect_config(self) -> AppConfig:
        config = AppConfig()
        config.account_email = self.email_entry.get_text().strip()
        config.account_name = self.name_entry.get_text().strip() or "AI 邮件助手"

        config.imap.host = self.imap_host.get_text().strip()
        config.imap.port = _int(self.imap_port.get_text(), 993)
        config.imap.security = SECURITY_ORDER[self.imap_security.get_selected()]
        config.imap.username = self.imap_user.get_text().strip()
        config.imap.password = self.imap_password.get_text()
        config.imap.mailbox = self.imap_mailbox.get_text().strip() or "INBOX"
        if not config.imap.username and config.account_email:
            config.imap.username = config.account_email

        config.smtp.host = self.smtp_host.get_text().strip()
        config.smtp.port = _int(self.smtp_port.get_text(), 465)
        config.smtp.security = SECURITY_ORDER[self.smtp_security.get_selected()]
        config.smtp.use_auth = self.smtp_auth.get_active()
        config.smtp.username = self.smtp_user.get_text().strip()
        config.smtp.password = self.smtp_password.get_text()

        config.rule.keywords = split_keywords(self.keywords_entry.get_text())
        subject, body = SCOPE_VALUES[self.scope_combo.get_selected()]
        config.rule.match_subject = subject
        config.rule.match_body = body
        config.rule.poll_seconds = max(5, int(self.poll_spin.get_value()))
        config.rule.ignore_auto_submitted = self.ignore_auto.get_active()
        config.rule.process_existing_unread = self.process_existing.get_active()
        config.rule.allowed_senders = split_addresses(self.allowed_entry.get_text())

        config.ai.base_url = normalize_base_url(self.ai_base.get_text())
        config.ai.endpoint_path = self.ai_path.get_text().strip() or "/v1/chat/completions"
        config.ai.model = self.ai_model.get_text().strip()
        config.ai.api_key = self.ai_key.get_text().strip()
        config.ai.system_prompt = self._system_prompt or ""
        config.ai.temperature = float(self.ai_temperature.get_value())
        config.ai.max_tokens = int(self.ai_max_tokens.get_value())
        config.ai.timeout_seconds = int(self.ai_timeout.get_value())
        config.ai.ignore_output_limit = self.ai_ignore_length.get_active()
        config.ai.ignore_timeout = self.ai_ignore_timeout.get_active()
        config.ai.use_backend_defaults = self.ai_use_backend_defaults.get_active()

        config.behavior.ack_text = self.ack_entry.get_text().strip() or "收到"
        config.behavior.send_error_notice = self.error_notice.get_active()
        return config

    # --------------------------------------------------------------- helpers
    def _update_endpoint_preview(self) -> None:
        try:
            endpoint = build_endpoint(
                self.ai_base.get_text(),
                self.ai_path.get_text() if hasattr(self, "ai_path") else "/v1/chat/completions",
            )
        except Exception:
            endpoint = "—"
        self.endpoint_row.set_subtitle(endpoint or "—")

    def _update_prompt_row(self) -> None:
        if not self._system_prompt:
            self.prompt_row.set_subtitle("（空，不发送 system 消息）")
        else:
            self.prompt_row.set_subtitle(_short(self._system_prompt, 58))

    def _sync_auth_sensitivity(self) -> None:
        active = self.smtp_auth.get_active()
        self.smtp_user.set_sensitive(active)
        self.smtp_password.set_sensitive(active)

    def _sync_ai_limits(self) -> None:
        self.ai_max_tokens.set_sensitive(not self.ai_ignore_length.get_active())
        self.ai_timeout.set_sensitive(not self.ai_ignore_timeout.get_active())
        self.ai_temperature.set_sensitive(not self.ai_use_backend_defaults.get_active())

    def _autofill_provider(self) -> None:
        email = self.email_entry.get_text().strip().lower()
        domain = email.rsplit("@", 1)[-1] if "@" in email else ""
        provider = PROVIDERS.get(domain)
        if not provider:
            self._show_toast("未识别这个邮箱域名，请手动填写服务器信息")
            return
        imap_host, imap_port, imap_security, smtp_host, smtp_port, smtp_security, name = provider
        self.imap_host.set_text(imap_host)
        self.imap_port.set_text(str(imap_port))
        self.imap_security.set_selected(SECURITY_ORDER.index(imap_security))
        self.smtp_host.set_text(smtp_host)
        self.smtp_port.set_text(str(smtp_port))
        self.smtp_security.set_selected(SECURITY_ORDER.index(smtp_security))
        if not self.imap_user.get_text().strip():
            self.imap_user.set_text(email)
        if not self.smtp_user.get_text().strip():
            self.smtp_user.set_text(email)
        self._show_toast(f"已填入 {name} 的服务器设置，请填写授权码/应用专用密码")
        self._update_endpoint_preview()

    def _prepare_imap_test(self):
        config = self.collect_config()
        problems = []
        if not config.imap.host:
            problems.append("IMAP 服务器")
        if not config.imap.username:
            problems.append("IMAP 用户名")
        if not config.imap.password:
            problems.append("IMAP 密码")
        if problems:
            self.imap_test_row.set_subtitle("请先填写：" + "、".join(problems))
            self._show_toast("IMAP 测试缺少必填项")
            return None
        return config

    def _test_imap(self) -> None:
        config = self._prepare_imap_test()
        if config is None:
            return
        self.imap_test_row.set_subtitle("正在连接…")
        self.imap_test_button.set_label("测试中…")
        self.imap_test_button.set_sensitive(False)
        run_async(
            lambda: test_imap(config.imap),
            lambda count: self._test_imap_done(True, f"✓ 连接成功，收件箱共 {count} 封邮件"),
            lambda exc: self._test_imap_done(False, f"✗ {_short(str(exc), 240)}"),
        )

    def _test_imap_done(self, ok: bool, message: str) -> None:
        self.imap_test_row.set_subtitle(message)
        self.imap_test_button.set_label("测试 IMAP 连接")
        self.imap_test_button.set_sensitive(True)
        self._show_toast("IMAP 连接成功" if ok else "IMAP 连接失败")

    def _prepare_smtp_test(self):
        config = self.collect_config()
        problems = []
        if not config.smtp.host:
            problems.append("SMTP 服务器")
        if config.smtp.use_auth and not (config.smtp.username or config.account_email):
            problems.append("SMTP 用户名")
        if config.smtp.use_auth and not config.smtp.password:
            problems.append("SMTP 密码")
        if problems:
            self.smtp_test_row.set_subtitle("请先填写：" + "、".join(problems))
            self._show_toast("SMTP 测试缺少必填项")
            return None
        return config

    def _test_smtp(self) -> None:
        config = self._prepare_smtp_test()
        if config is None:
            return
        self.smtp_test_row.set_subtitle("正在连接…")
        self.smtp_test_button.set_label("测试中…")
        self.smtp_test_button.set_sensitive(False)

        def probe():
            with SmtpSender(config.smtp, config.account_email, config.account_name):
                return True

        run_async(
            probe,
            lambda _ok: self._test_smtp_done(True, "✓ 连接成功，可以发送邮件"),
            lambda exc: self._test_smtp_done(False, f"✗ {_short(str(exc), 240)}"),
        )

    def _test_smtp_done(self, ok: bool, message: str) -> None:
        self.smtp_test_row.set_subtitle(message)
        self.smtp_test_button.set_label("测试 SMTP 连接")
        self.smtp_test_button.set_sensitive(True)
        self._show_toast("SMTP 连接成功" if ok else "SMTP 连接失败")

    def _send_test_mail(self) -> None:
        config = self._prepare_smtp_test()
        if config is None:
            return
        self.smtp_send_row.set_subtitle("正在发送…")
        self.smtp_send_button.set_label("发送中…")
        self.smtp_send_button.set_sensitive(False)

        def send():
            with SmtpSender(config.smtp, config.account_email, config.account_name) as sender:
                sender.send_test()

        run_async(
            send,
            lambda _ok: self._send_test_done(True, f"✓ 测试邮件已发送到 {config.account_email}"),
            lambda exc: self._send_test_done(False, f"✗ {_short(str(exc), 240)}"),
        )

    def _send_test_done(self, ok: bool, message: str) -> None:
        self.smtp_send_row.set_subtitle(message)
        self.smtp_send_button.set_label("发送到我的邮箱")
        self.smtp_send_button.set_sensitive(True)
        self._show_toast("测试邮件已发送" if ok else "测试邮件发送失败")

    def _test_ai(self) -> None:
        config = self.collect_config()
        if not config.ai.base_url:
            self.ai_test_row.set_subtitle("请先填写 AI API 基础地址")
            self._show_toast("AI 测试缺少接口地址")
            return
        self.ai_test_row.set_subtitle("正在请求 AI（可能需要几秒）…")
        self.ai_test_button.set_label("测试中…")
        self.ai_test_button.set_sensitive(False)
        run_async(
            lambda: AIClient(config.ai).test_connection(),
            lambda answer: self._test_ai_done(True, f"✓ {_short(answer, 120)}"),
            lambda exc: self._test_ai_done(False, f"✗ {_short(str(exc), 240)}"),
        )

    def _test_ai_done(self, ok: bool, message: str) -> None:
        self.ai_test_row.set_subtitle(message)
        self.ai_test_button.set_label("测试 AI 接口")
        self.ai_test_button.set_sensitive(True)
        self._show_toast("AI 接口连接成功" if ok else "AI 接口连接失败")

    def _edit_prompt(self) -> None:
        if self._prompt_dialog is not None:
            self._prompt_dialog.present(self.get_root())
            return
        dialog = PromptDialog(self._system_prompt, self._prompt_saved)
        dialog.connect("closed", self._prompt_closed)
        self._prompt_dialog = dialog
        dialog.present(self.get_root())

    def _prompt_closed(self, _dialog) -> None:
        self._prompt_dialog = None

    def _prompt_saved(self, text: str) -> None:
        self._system_prompt = text
        self._update_prompt_row()
        self._show_toast("系统提示词已更新，保存设置后生效")

    def _confirm_reset(self) -> None:
        dialog = Adw.AlertDialog(
            heading="清空所有配置？",
            body="将删除本地设置和运行状态。加密密钥会保留，已发送/收到的邮件不受影响。",
        )
        dialog.add_response("cancel", "取消")
        dialog.add_response("clear", "清空")
        dialog.set_response_appearance("clear", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _dialog, response: self._on_reset() if response == "clear" else None)
        dialog.present(self.get_root())

    def set_banner(self, title: str, revealed: bool) -> None:
        self.banner.set_title(title)
        if revealed:
            self.banner.set_visible(True)
            self.banner.set_revealed(True)
        else:
            self.banner.set_revealed(False)
            self.banner.set_visible(False)
