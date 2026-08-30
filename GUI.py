import random
import sys
import threading
from datetime import datetime

from PyQt5.QtCore import QThread, QTimer, Qt, pyqtSignal
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

import api_ttc as core


def _identity(account):
    email = str(account.get("email", "")).strip()
    return email or f"Manual token {core.preview_token(account.get('token', ''))}"


class JobWorker(QThread):
    """Qt thread wrapper around one core-owned account runtime."""

    event_signal = pyqtSignal(str, str, object)

    def __init__(self, profile_id, auth_token, account, config,
                 runtime_factory=None, parent=None):
        super().__init__(parent)
        self.profile_id = str(profile_id)
        self.account = dict(account)
        self.account_key = core.facebook_account_key(self.account)
        self.auth_token = str(auth_token)
        self.config = dict(config)
        self.runtime_factory = runtime_factory or core.TTCAccountRuntime
        self.stop_event = threading.Event()
        self.runtime = None
        self.result = None
        self.error = None

    def request_stop(self):
        self.stop_event.set()
        if self.runtime is not None:
            self.runtime.request_stop()

    stop = request_stop

    def _forward_event(self, event):
        self.event_signal.emit(self.profile_id, self.account_key, dict(event))

    def run(self):
        try:
            self.runtime = self.runtime_factory(
                profile_id=self.profile_id,
                auth_token=self.auth_token,
                account=dict(self.account),
                config=dict(self.config),
                worker_id=f"{self.profile_id}:{self.account_key[-12:]}",
                stop_event=self.stop_event,
                event_callback=self._forward_event,
            )
            self.result = self.runtime.run()
        except Exception as exc:
            self.error = exc
            self._forward_event({
                "type": "error",
                "message": str(exc),
                "stage": "worker",
            })


class CoreOperationWorker(QThread):
    result_ready = pyqtSignal(object)
    error_ready = pyqtSignal(str)

    def __init__(self, operation, parent=None):
        super().__init__(parent)
        self.operation = operation

    def run(self):
        try:
            self.result_ready.emit(self.operation())
        except Exception as exc:
            self.error_ready.emit(str(exc))


class AccountCard(QGroupBox):
    run_clicked = pyqtSignal(str)
    settings_clicked = pyqtSignal(str)
    delete_clicked = pyqtSignal(str)

    def __init__(self, account_key, account, enabled=True, parent=None):
        super().__init__(parent)
        self.account_key = account_key
        self.account = dict(account)
        self._running = False
        self._build()
        self.update_account(account, enabled)

    def _build(self):
        layout = QGridLayout(self)
        self.identity_label = QLabel()
        self.uid_label = QLabel("UID: chưa resolve")
        self.jobs_label = QLabel("Session: 0 job | 0 xu")
        self.status_label = QLabel("Trạng thái: Idle")
        self.enabled_label = QLabel()

        layout.addWidget(self.identity_label, 0, 0, 1, 2)
        layout.addWidget(self.enabled_label, 0, 2)
        layout.addWidget(self.uid_label, 1, 0)
        layout.addWidget(self.jobs_label, 1, 1)
        layout.addWidget(self.status_label, 1, 2)

        self.run_button = QPushButton("▶ Chạy")
        self.settings_button = QPushButton("⚙ Cài đặt")
        self.delete_button = QPushButton("✕ Xóa")
        self.delete_button.setObjectName("dangerButton")
        self.run_button.clicked.connect(lambda: self.run_clicked.emit(self.account_key))
        self.settings_button.clicked.connect(
            lambda: self.settings_clicked.emit(self.account_key)
        )
        self.delete_button.clicked.connect(lambda: self.delete_clicked.emit(self.account_key))
        layout.addWidget(self.run_button, 2, 0)
        layout.addWidget(self.settings_button, 2, 1)
        layout.addWidget(self.delete_button, 2, 2)

    def update_account(self, account, enabled):
        self.account = dict(account)
        self.identity_label.setText(f"Tài khoản: {_identity(account)}")
        self.enabled_label.setText("Start All: ON" if enabled else "Start All: OFF")
        self.enabled_label.setStyleSheet(
            "color: #7ee787;" if enabled else "color: #9aa4b2;"
        )

    def set_runtime(self, status=None, uid=None, jobs=None, coin=None):
        if status is not None:
            self.status_label.setText(f"Trạng thái: {status}")
        if uid:
            self.uid_label.setText(f"UID: {uid}")
        if jobs is not None or coin is not None:
            current_jobs = 0 if jobs is None else int(jobs)
            current_coin = 0 if coin is None else int(coin)
            self.jobs_label.setText(
                f"Session: {current_jobs} job | {core.format_coin(current_coin)} xu"
            )

    def set_running(self, running, stopping=False):
        self._running = bool(running)
        if stopping:
            self.run_button.setText("Đang dừng…")
            self.run_button.setEnabled(False)
            self.set_runtime(status="Stopping")
        elif running:
            self.run_button.setText("⏹ Dừng")
            self.run_button.setEnabled(True)
            self.set_runtime(status="Running")
        else:
            self.run_button.setText("▶ Chạy")
            self.run_button.setEnabled(True)

    def set_mutation_enabled(self, enabled):
        self.settings_button.setEnabled(enabled)
        self.delete_button.setEnabled(enabled)

    def set_run_enabled(self, enabled):
        if not self._running:
            self.run_button.setEnabled(enabled)


class ProfileSummaryCard(QGroupBox):
    clicked = pyqtSignal(str)
    run_toggle_clicked = pyqtSignal(str)

    def __init__(self, profile_id, parent=None):
        super().__init__(parent)
        self.profile_id = str(profile_id)
        layout = QGridLayout(self)
        self.name_label = QLabel()
        self.status_label = QLabel()
        self.run_button = QPushButton()
        self.run_button.setMinimumWidth(96)
        self.run_button.clicked.connect(
            lambda: self.run_toggle_clicked.emit(self.profile_id)
        )
        self.count_label = QLabel()
        self.session_label = QLabel()
        self.balance_label = QLabel()
        self.running_label = QLabel()
        self.log_label = QLabel()
        self.log_label.setWordWrap(True)
        layout.addWidget(self.name_label, 0, 0)
        layout.addWidget(self.status_label, 0, 1, Qt.AlignRight)
        layout.addWidget(self.run_button, 0, 2, Qt.AlignRight)
        layout.addWidget(self.count_label, 1, 0)
        layout.addWidget(self.session_label, 1, 1, 1, 2, Qt.AlignRight)
        layout.addWidget(self.balance_label, 2, 0)
        layout.addWidget(self.running_label, 2, 1, 1, 2, Qt.AlignRight)
        layout.addWidget(self.log_label, 3, 0, 1, 3)
        self.setCursor(Qt.PointingHandCursor)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.profile_id)
        super().mousePressEvent(event)

    def update_summary(self, *, name, status, account_count, active_count,
                       pending_count, jobs, coin_text, balance_text, identities,
                       latest_log, selected, run_text, run_enabled,
                       run_tooltip=""):
        self.setTitle("Đang xem" if selected else "")
        self.name_label.setText(f"Profile: {name}")
        self.status_label.setText(status.upper())
        status_colors = {
            "Running": "#7ee787", "Stopping": "#f2cc60",
            "Pending": "#79c0ff", "Error": "#ff7b72", "Idle": "#9aa4b2",
        }
        self.status_label.setStyleSheet(
            f"color: {status_colors.get(status, '#9aa4b2')}; font-weight: bold;"
        )
        self.run_button.setText(run_text)
        self.run_button.setEnabled(run_enabled)
        self.run_button.setToolTip(run_tooltip)
        pending_text = f" | Chờ: {pending_count}" if pending_count else ""
        self.count_label.setText(
            f"FB: {account_count} | Đang chạy: {active_count}{pending_text}"
        )
        self.session_label.setText(
            f"Session: {jobs} job | +{coin_text} xu"
        )
        self.balance_label.setText(f"Số dư TTC: {balance_text}")
        self.running_label.setText(
            f"Đang chạy: {identities}" if identities else "Đang chạy: —"
        )
        self.log_label.setText(
            f"Log mới nhất: {latest_log or 'Chưa có log trong phiên này'}"
        )
        border = "#58a6ff" if selected else "#44505f"
        self.setStyleSheet(
            f"QGroupBox {{ border: 2px solid {border}; border-radius: 7px; "
            "margin-top: 9px; padding-top: 9px; }"
        )


class SelectAccountsDialog(QDialog):
    def __init__(self, source_profiles, parent=None):
        super().__init__(parent)
        self.source_profiles = [dict(item) for item in source_profiles]
        self.account_checks = []
        self.setWindowTitle("Nhập Facebook account từ profile khác")
        self.resize(580, 420)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.source_combo = QComboBox()
        for profile in self.source_profiles:
            self.source_combo.addItem(
                f"{profile['name']} — {len(profile.get('fb_accounts', []))} Facebook",
                profile["profile_id"],
            )
        form.addRow("Profile nguồn:", self.source_combo)
        layout.addLayout(form)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.accounts_container = QWidget()
        self.accounts_layout = QVBoxLayout(self.accounts_container)
        self.accounts_layout.setAlignment(Qt.AlignTop)
        scroll.setWidget(self.accounts_container)
        layout.addWidget(scroll, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.source_combo.currentIndexChanged.connect(self._render_accounts)
        self._render_accounts()

    def _render_accounts(self):
        while self.accounts_layout.count():
            item = self.accounts_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.account_checks = []
        profile_id = self.source_combo.currentData()
        profile = next(
            (item for item in self.source_profiles
             if item.get("profile_id") == profile_id),
            None,
        )
        for account in (profile or {}).get("fb_accounts", []):
            account_key = core.facebook_account_key(account)
            if not account_key:
                continue
            checkbox = QCheckBox(_identity(account))
            checkbox.setProperty("account_key", account_key)
            self.accounts_layout.addWidget(checkbox)
            self.account_checks.append(checkbox)
        if not self.account_checks:
            self.accounts_layout.addWidget(QLabel("Profile nguồn không có Facebook account."))

    def selection(self):
        return self.source_combo.currentData(), [
            checkbox.property("account_key")
            for checkbox in self.account_checks if checkbox.isChecked()
        ]


class AddAccountDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Thêm Facebook account")
        self.resize(560, 390)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.mode = QComboBox()
        self.mode.addItem("Login một account", "login")
        self.mode.addItem("Token thủ công", "manual")
        self.mode.addItem("Batch UID|PASSWORD|2FA", "batch")
        self.email = QLineEdit()
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        self.secret = QLineEdit()
        self.secret.setEchoMode(QLineEdit.Password)
        self.token = QLineEdit()
        self.token.setEchoMode(QLineEdit.Password)
        self.batch = QPlainTextEdit()
        self.batch.setPlaceholderText("UID|PASSWORD|2FA,UID|PASSWORD|2FA")
        form.addRow("Cách thêm:", self.mode)
        form.addRow("UID / email:", self.email)
        form.addRow("Password:", self.password)
        form.addRow("2FA:", self.secret)
        form.addRow("Facebook token:", self.token)
        form.addRow("Danh sách batch:", self.batch)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.mode.currentIndexChanged.connect(self._sync_mode)
        self._sync_mode()

    def _sync_mode(self):
        mode = self.mode.currentData()
        for widget in (self.email, self.password, self.secret):
            widget.setEnabled(mode == "login")
        self.token.setEnabled(mode == "manual")
        self.batch.setEnabled(mode == "batch")

    def values(self):
        return {
            "mode": self.mode.currentData(),
            "email": self.email.text().strip(),
            "password": self.password.text(),
            "secret": self.secret.text().strip(),
            "token": self.token.text().strip(),
            "batch": self.batch.toPlainText(),
        }


class AccountSettingsDialog(QDialog):
    def __init__(self, account, enabled, parent=None):
        super().__init__(parent)
        self.action = None
        self.account = dict(account)
        self.setWindowTitle("Cài đặt Facebook account")
        self.resize(520, 360)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.addRow("Identity:", QLabel(_identity(account)))
        form.addRow("Token:", QLabel(core.preview_token(account.get("token", ""))))
        self.email = QLineEdit(str(account.get("email", "")))
        self.token = QLineEdit()
        self.token.setEchoMode(QLineEdit.Password)
        self.token.setPlaceholderText("Để trống để giữ token hiện tại")
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        self.password.setPlaceholderText("Để trống để giữ password hiện tại")
        self.secret = QLineEdit()
        self.secret.setEchoMode(QLineEdit.Password)
        self.secret.setPlaceholderText("Để trống để giữ 2FA hiện tại")
        self.clear_password = QCheckBox("Xóa password đã lưu")
        self.clear_secret = QCheckBox("Xóa 2FA đã lưu")
        self.enabled = QCheckBox("Bật account cho Start All")
        self.enabled.setChecked(enabled)
        form.addRow("UID / email:", self.email)
        form.addRow("Token mới:", self.token)
        form.addRow("Password mới:", self.password)
        form.addRow("", self.clear_password)
        form.addRow("2FA mới:", self.secret)
        form.addRow("", self.clear_secret)
        form.addRow("", self.enabled)
        layout.addLayout(form)

        row = QHBoxLayout()
        save_button = QPushButton("Lưu")
        relogin_button = QPushButton("Login / relogin lấy token")
        close_button = QPushButton("Đóng")
        save_button.clicked.connect(lambda: self._finish("save"))
        relogin_button.clicked.connect(lambda: self._finish("relogin"))
        close_button.clicked.connect(self.reject)
        row.addWidget(save_button)
        row.addWidget(relogin_button)
        row.addStretch()
        row.addWidget(close_button)
        layout.addLayout(row)

    def _finish(self, action):
        self.action = action
        self.accept()

    def updates(self):
        updates = {"email": self.email.text().strip()}
        if self.token.text().strip():
            updates["token"] = self.token.text().strip()
        if self.clear_password.isChecked():
            updates["password"] = ""
        elif self.password.text():
            updates["password"] = self.password.text()
        if self.clear_secret.isChecked():
            updates["secret"] = ""
        elif self.secret.text():
            updates["secret"] = self.secret.text().strip()
        return updates


class ProfileDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Tạo TTC profile")
        layout = QFormLayout(self)
        self.name = QLineEdit()
        self.token = QLineEdit()
        self.token.setEchoMode(QLineEdit.Password)
        layout.addRow("Tên profile:", self.name)
        layout.addRow("TTC token:", self.token)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)


class MainWindow(QMainWindow):
    def __init__(self, runtime_factory=None, core_module=None):
        super().__init__()
        self.core = core_module or core
        self.runtime_factory = runtime_factory
        self.workers = {}
        self.pending_starts = set()
        self._start_generation = 0
        self.profile_lease = None
        self.run_profile_id = None
        self.view_profile_id = None
        self.profile_id = None
        self.profile = None
        self.profiles_by_id = {}
        self.profile_cards = {}
        self.accounts_by_key = {}
        self.account_widgets = {}
        self.session_metrics = {}
        self.account_last_metrics = {}
        self.completed_profile_metrics = {}
        self.resolved_uids = {}
        self.account_statuses = {}
        self.latest_profile_log = {}
        self.profile_balances = {}
        self.profile_errors = set()
        self._operations = set()
        self._loading = False
        self._config_loading = False
        self._config_dirty = False
        self._closing = False

        self.setWindowTitle("TTC Auto Tool — Multi-worker")
        self.resize(1040, 780)
        self._build_ui()
        self._apply_styles()
        self.reload_from_core()

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        self.tabs = QTabWidget()
        root.addWidget(self.tabs)

        accounts_tab = QWidget()
        accounts_layout = QVBoxLayout(accounts_tab)

        self.accounts_stack = QStackedWidget()
        accounts_layout.addWidget(self.accounts_stack, 1)

        self.profile_overview_page = QWidget()
        overview_layout = QVBoxLayout(self.profile_overview_page)
        overview_toolbar = QHBoxLayout()
        overview_title = QLabel("TỔNG QUAN PROFILE")
        overview_title.setStyleSheet("font-size: 15px; font-weight: bold;")
        self.new_profile_button = QPushButton("＋ Tạo profile")
        self.overview_refresh_button = QPushButton("↻")
        self.new_profile_button.clicked.connect(self.create_profile)
        self.overview_refresh_button.clicked.connect(self.reload_from_core)
        overview_toolbar.addWidget(overview_title)
        overview_toolbar.addStretch()
        overview_toolbar.addWidget(self.new_profile_button)
        overview_toolbar.addWidget(self.overview_refresh_button)
        overview_layout.addLayout(overview_toolbar)
        self.overview_scroll = QScrollArea()
        self.overview_scroll.setWidgetResizable(True)
        self.profile_cards_container = QWidget()
        self.profile_cards_layout = QVBoxLayout(self.profile_cards_container)
        self.profile_cards_layout.setAlignment(Qt.AlignTop)
        self.overview_scroll.setWidget(self.profile_cards_container)
        overview_layout.addWidget(self.overview_scroll, 1)
        self.accounts_stack.addWidget(self.profile_overview_page)

        self.profile_detail_page = QWidget()
        detail_layout = QVBoxLayout(self.profile_detail_page)
        detail_header = QHBoxLayout()
        self.back_to_overview_button = QPushButton("← Tổng quan profile")
        self.back_to_overview_button.clicked.connect(self._show_profile_overview)
        self.detail_title_label = QLabel("Chi tiết profile")
        self.detail_title_label.setStyleSheet("font-size: 15px; font-weight: bold;")
        detail_header.addWidget(self.back_to_overview_button)
        detail_header.addWidget(self.detail_title_label)
        detail_header.addStretch()
        detail_layout.addLayout(detail_header)

        profile_row = QHBoxLayout()
        profile_row.addWidget(QLabel("Profile đang xem:"))
        self.profile_combo = QComboBox()
        self.profile_combo.currentIndexChanged.connect(self._profile_changed)
        profile_row.addWidget(self.profile_combo, 1)
        self.rename_profile_button = QPushButton("Đổi tên")
        self.copy_accounts_button = QPushButton("Sao chép FB")
        self.delete_profile_button = QPushButton("Xóa profile")
        self.delete_profile_button.setObjectName("dangerButton")
        self.refresh_button = QPushButton("↻")
        self.rename_profile_button.clicked.connect(self.rename_profile)
        self.copy_accounts_button.clicked.connect(self.copy_accounts)
        self.delete_profile_button.clicked.connect(self.delete_profile)
        self.refresh_button.clicked.connect(self.reload_from_core)
        for button in (
            self.rename_profile_button,
            self.copy_accounts_button,
            self.delete_profile_button,
            self.refresh_button,
        ):
            profile_row.addWidget(button)
        detail_layout.addLayout(profile_row)

        add_row = QHBoxLayout()
        self.add_account_button = QPushButton("＋ Thêm Facebook")
        self.add_account_button.clicked.connect(self.add_account)
        self.aggregate_label = QLabel("Active: 0 | Tổng session: 0 job")
        add_row.addWidget(self.add_account_button)
        add_row.addStretch()
        add_row.addWidget(self.aggregate_label)
        detail_layout.addLayout(add_row)

        self.accounts_scroll = QScrollArea()
        self.accounts_scroll.setWidgetResizable(True)
        self.cards_container = QWidget()
        self.cards_layout = QVBoxLayout(self.cards_container)
        self.cards_layout.setAlignment(Qt.AlignTop)
        self.accounts_scroll.setWidget(self.cards_container)
        detail_layout.addWidget(self.accounts_scroll, 1)

        run_row = QHBoxLayout()
        self.start_all_button = QPushButton("▶ Chạy tất cả đã bật")
        self.stop_all_button = QPushButton("⏹ Dừng tất cả")
        self.start_all_button.clicked.connect(self.start_all)
        self.stop_all_button.clicked.connect(self.stop_all)
        run_row.addWidget(self.start_all_button)
        run_row.addWidget(self.stop_all_button)
        run_row.addStretch()
        detail_layout.addLayout(run_row)
        self.accounts_stack.addWidget(self.profile_detail_page)
        self.accounts_stack.setCurrentWidget(self.profile_overview_page)
        self.tabs.addTab(accounts_tab, "Tài khoản")

        logs_tab = QWidget()
        logs_layout = QVBoxLayout(logs_tab)
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setFont(QFont("Consolas", 10))
        clear_button = QPushButton("Xóa log")
        clear_button.clicked.connect(self.log_text.clear)
        logs_layout.addWidget(self.log_text)
        logs_layout.addWidget(clear_button)
        self.tabs.addTab(logs_tab, "Nhật ký")

        settings_tab = QWidget()
        settings_layout = QVBoxLayout(settings_tab)
        token_group = QGroupBox("TTC profile hiện tại")
        token_row = QHBoxLayout(token_group)
        self.profile_summary = QLabel("Chưa có profile")
        self.update_ttc_token_button = QPushButton("Cập nhật TTC token")
        self.balance_button = QPushButton("Kiểm tra số dư")
        self.jobs_button = QPushButton("Xem jobs")
        self.update_ttc_token_button.clicked.connect(self.update_ttc_token)
        self.balance_button.clicked.connect(self.check_balance)
        self.jobs_button.clicked.connect(self.view_jobs)
        token_row.addWidget(self.profile_summary, 1)
        token_row.addWidget(self.update_ttc_token_button)
        token_row.addWidget(self.balance_button)
        token_row.addWidget(self.jobs_button)
        settings_layout.addWidget(token_group)

        config_group = QGroupBox("Cấu hình chạy")
        config_grid = QGridLayout(config_group)
        self.job_types_combo = QComboBox()
        self.job_types_combo.addItem("Chỉ Like", ("like_facebook",))
        self.job_types_combo.addItem("Chỉ Comment", ("like_poster",))
        self.job_types_combo.addItem("Like + Comment", ("like_facebook", "like_poster"))
        self.continuous_check = QCheckBox("Chạy liên tục")
        self.skip_hidden_check = QCheckBox("Bỏ qua comment bị ẩn")
        self.max_jobs_spin = self._spin(1, 100000)
        config_grid.addWidget(QLabel("Loại nhiệm vụ"), 0, 0)
        config_grid.addWidget(self.job_types_combo, 0, 1)
        config_grid.addWidget(QLabel("Số nhiệm vụ tối đa / tài khoản"), 0, 2)
        config_grid.addWidget(self.max_jobs_spin, 0, 3)
        config_grid.addWidget(self.continuous_check, 1, 0, 1, 2)
        config_grid.addWidget(self.skip_hidden_check, 1, 2, 1, 2)

        self.config_spins = {}
        rows = (
            ("like_batch", "Số Like mỗi lượt", 1),
            ("comment_batch", "Số Comment mỗi lượt", 1),
            ("like_delay", "Nghỉ giữa các Like (giây)", 0),
            ("comment_delay", "Nghỉ giữa các Comment (giây)", 0),
            ("failed_delay", "Nghỉ sau job lỗi (giây)", 0),
            ("account_delay", "Giãn cách khởi chạy tài khoản (giây)", 0),
        )
        for row_index, (key, label, minimum) in enumerate(rows, start=2):
            minimum_spin = self._spin(minimum, 86400)
            maximum_spin = self._spin(minimum, 86400)
            self.config_spins[key + "_min"] = minimum_spin
            self.config_spins[key + "_max"] = maximum_spin
            config_grid.addWidget(QLabel(label), row_index, 0)
            config_grid.addWidget(minimum_spin, row_index, 1)
            config_grid.addWidget(QLabel("đến"), row_index, 2)
            config_grid.addWidget(maximum_spin, row_index, 3)
        self.save_config_button = QPushButton("Lưu cấu hình")
        self.save_config_button.clicked.connect(self.save_run_config)
        self.config_status_label = QLabel()
        config_grid.addWidget(self.config_status_label, len(rows) + 2, 0, 1, 3)
        config_grid.addWidget(self.save_config_button, len(rows) + 2, 3)
        self.job_types_combo.currentIndexChanged.connect(self._mark_config_dirty)
        self.continuous_check.toggled.connect(self._mark_config_dirty)
        self.skip_hidden_check.toggled.connect(self._mark_config_dirty)
        self.max_jobs_spin.valueChanged.connect(self._mark_config_dirty)
        for spin in self.config_spins.values():
            spin.valueChanged.connect(self._mark_config_dirty)
        settings_layout.addWidget(config_group)
        settings_layout.addStretch()
        self.tabs.addTab(settings_tab, "Cài đặt")

    @staticmethod
    def _spin(minimum, maximum):
        widget = QSpinBox()
        widget.setRange(minimum, maximum)
        return widget

    def _apply_styles(self):
        self.setStyleSheet("""
            QMainWindow, QWidget { background: #20242a; color: #e6edf3; }
            QTabWidget::pane, QGroupBox { border: 1px solid #44505f; }
            QGroupBox { border-radius: 7px; margin-top: 9px; padding-top: 9px; }
            QGroupBox::title { subcontrol-origin: margin; left: 9px; padding: 0 4px; }
            QLineEdit, QTextEdit, QPlainTextEdit, QSpinBox, QComboBox {
                background: #2d333b; color: #e6edf3; border: 1px solid #57606a;
                border-radius: 4px; padding: 5px;
            }
            QPushButton { background: #316dca; color: white; border: 0;
                border-radius: 4px; padding: 6px 10px; }
            QPushButton:hover { background: #4184e4; }
            QPushButton:disabled { background: #444c56; color: #9aa4b2; }
            QPushButton#dangerButton { background: #b62324; }
            QTabBar::tab { background: #2d333b; padding: 7px 14px; }
            QTabBar::tab:selected { background: #316dca; }
        """)

    @staticmethod
    def _runtime_key(profile_id, account_key):
        return str(profile_id), str(account_key)

    def _account_for_runtime(self, profile_id, account_key):
        profile = self.profiles_by_id.get(str(profile_id), {})
        return next(
            (item for item in profile.get("fb_accounts", [])
             if self.core.facebook_account_key(item) == account_key),
            {},
        )

    def log(self, message, account_key=None, profile_id=None):
        timestamp = datetime.now().strftime("%H:%M:%S")
        prefix = ""
        profile_id = str(profile_id) if profile_id is not None else None
        profile = self.profiles_by_id.get(profile_id, {}) if profile_id else {}
        if profile_id:
            prefix += f"[{profile.get('name', profile_id)}] "
        if account_key and profile_id:
            account = self._account_for_runtime(profile_id, account_key)
            prefix += f"[{_identity(account)}] "
        elif account_key:
            account = self.accounts_by_key.get(account_key, {})
            prefix += f"[{_identity(account)}] "
        formatted = f"[{timestamp}] {prefix}{message}"
        self.log_text.append(formatted)
        if profile_id:
            self.latest_profile_log[profile_id] = formatted
            self._update_profile_card(profile_id)

    def reload_from_core(self):
        try:
            data = self.core.load_database()
        except Exception as exc:
            QMessageBox.critical(self, "Lỗi dữ liệu", str(exc))
            return
        self._loading = True
        try:
            previous_id = self.view_profile_id
            profiles = data.get("profiles", [])
            self.profiles_by_id = {
                str(item["profile_id"]): item for item in profiles
            }
            valid_ids = set(self.profiles_by_id)
            active_id = data.get("active_profile_id")
            active_id = str(active_id) if active_id is not None else None
            selected_id = previous_id if previous_id in valid_ids else active_id
            self.profile_combo.blockSignals(True)
            self.profile_combo.clear()
            for profile in profiles:
                count = len(profile.get("fb_accounts", []))
                self.profile_combo.addItem(
                    f"{profile['name']} — {count} Facebook", profile["profile_id"]
                )
            index = self.profile_combo.findData(selected_id)
            if index >= 0:
                self.profile_combo.setCurrentIndex(index)
            self.profile_combo.blockSignals(False)
            self.view_profile_id = selected_id if selected_id in valid_ids else None
            self.profile_id = self.view_profile_id
            self.profile = self.profiles_by_id.get(self.view_profile_id)
            self._render_dashboard()
            self._render_profile()
        finally:
            self._loading = False
        self._update_controls()

    def _render_dashboard(self):
        while self.profile_cards_layout.count():
            item = self.profile_cards_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.profile_cards = {}
        for profile_id in self.profiles_by_id:
            card = ProfileSummaryCard(profile_id)
            card.clicked.connect(self._view_profile)
            card.run_toggle_clicked.connect(self._toggle_profile_run)
            self.profile_cards_layout.addWidget(card)
            self.profile_cards[profile_id] = card
            self._update_profile_card(profile_id)

    def _profile_totals(self, profile_id):
        completed = self.completed_profile_metrics.get(profile_id, {})
        jobs = int(completed.get("jobs_succeeded", 0))
        coin = int(completed.get("session_coin", 0))
        for (runtime_profile_id, _account_key), metrics in self.session_metrics.items():
            if runtime_profile_id != profile_id:
                continue
            jobs += int(metrics.get("jobs_succeeded", 0))
            coin += int(metrics.get("session_coin", 0))
        return jobs, coin

    def _profile_status(self, profile_id, active_keys, pending_keys):
        if profile_id == self.run_profile_id:
            if any(self.workers[key].stop_event.is_set() for key in active_keys):
                return "Stopping"
            if active_keys:
                return "Running"
            if pending_keys:
                return "Pending"
        if profile_id in self.profile_errors:
            return "Error"
        return "Idle"

    def _update_profile_card(self, profile_id):
        card = self.profile_cards.get(profile_id)
        profile = self.profiles_by_id.get(profile_id)
        if card is None or profile is None:
            return
        active_keys = [key for key in self.workers if key[0] == profile_id]
        pending_keys = [key for key in self.pending_starts if key[0] == profile_id]
        identities = []
        for runtime_key in active_keys[:3]:
            uid = self.resolved_uids.get(runtime_key)
            account = self._account_for_runtime(*runtime_key)
            identities.append(str(uid or _identity(account)))
        remaining = len(active_keys) - len(identities)
        if remaining > 0:
            identities.append(f"+{remaining} khác")
        jobs, coin = self._profile_totals(profile_id)
        balance = self.profile_balances.get(profile_id)
        balance_text = (
            "—" if balance is None else f"{self.core.format_coin(balance)} xu"
        )
        status = self._profile_status(profile_id, active_keys, pending_keys)
        if status == "Stopping":
            run_text = "Đang dừng…"
            run_enabled = False
            run_tooltip = ""
        elif active_keys or pending_keys:
            run_text = "⏹ Dừng"
            run_enabled = True
            run_tooltip = ""
        else:
            run_text = "▶ Chạy"
            has_runnable_accounts = bool(self.core.selected_accounts(profile))
            other_profile_running = (
                self.run_profile_id is not None
                and self.run_profile_id != profile_id
            )
            run_enabled = (
                has_runnable_accounts
                and not other_profile_running
                and not self._operations
            )
            if other_profile_running:
                run_tooltip = "Profile khác đang chạy"
            elif not has_runnable_accounts:
                run_tooltip = "Profile không có Facebook account nào đang bật để chạy"
            else:
                run_tooltip = ""
        card.update_summary(
            name=profile.get("name", profile_id),
            status=status,
            account_count=len(profile.get("fb_accounts", [])),
            active_count=len(active_keys),
            pending_count=len(pending_keys),
            jobs=jobs,
            coin_text=self.core.format_coin(coin),
            balance_text=balance_text,
            identities=", ".join(identities),
            latest_log=self.latest_profile_log.get(profile_id),
            selected=profile_id == self.view_profile_id,
            run_text=run_text,
            run_enabled=run_enabled,
            run_tooltip=run_tooltip,
        )

    def _update_dashboard(self):
        for profile_id in self.profile_cards:
            self._update_profile_card(profile_id)

    def _show_profile_overview(self):
        self.accounts_stack.setCurrentWidget(self.profile_overview_page)

    def _view_profile(self, profile_id):
        profile_id = str(profile_id or "")
        if self._loading or profile_id not in self.profiles_by_id:
            return
        self.view_profile_id = profile_id
        self.profile_id = profile_id
        self.profile = self.profiles_by_id[profile_id]
        index = self.profile_combo.findData(profile_id)
        if index >= 0 and index != self.profile_combo.currentIndex():
            self.profile_combo.blockSignals(True)
            self.profile_combo.setCurrentIndex(index)
            self.profile_combo.blockSignals(False)
        self._render_profile()
        self._update_dashboard()
        self._update_controls()
        self.accounts_stack.setCurrentWidget(self.profile_detail_page)

    def _toggle_profile_run(self, profile_id):
        profile_id = str(profile_id or "").strip()
        if not profile_id:
            return
        has_runtime = any(
            key[0] == profile_id
            for key in (*self.workers, *self.pending_starts)
        )
        if has_runtime:
            return self.stop_profile(profile_id)
        return self.start_profile(profile_id)

    def _render_profile(self):
        while self.cards_layout.count():
            item = self.cards_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.account_widgets = {}
        self.accounts_by_key = {}

        if self.profile is None:
            self.detail_title_label.setText("Chi tiết profile")
            self.profile_summary.setText("Chưa có TTC profile")
            self._set_run_config_widgets(self.core.normalize_run_config())
            return
        self.detail_title_label.setText(f"Chi tiết: {self.profile['name']}")
        config = self.core.normalize_run_config(self.profile.get("run_config"))
        selected = set(self.core.selected_account_keys(self.profile))
        for account in self.profile.get("fb_accounts", []):
            key = self.core.facebook_account_key(account)
            if not key:
                continue
            self.accounts_by_key[key] = dict(account)
            card = AccountCard(key, account, key in selected)
            card.run_clicked.connect(self.on_run_account)
            card.settings_clicked.connect(self.open_account_settings)
            card.delete_clicked.connect(self.delete_account)
            self.cards_layout.addWidget(card)
            self.account_widgets[key] = card
            runtime_key = self._runtime_key(self.view_profile_id, key)
            metrics = self.session_metrics.get(
                runtime_key, self.account_last_metrics.get(runtime_key, {})
            )
            card.set_runtime(
                uid=self.resolved_uids.get(runtime_key),
                jobs=metrics.get("jobs_succeeded", 0),
                coin=metrics.get("session_coin", 0),
            )
            status = self.account_statuses.get(runtime_key)
            if runtime_key in self.workers:
                card.set_running(
                    True, stopping=self.workers[runtime_key].stop_event.is_set()
                )
            elif runtime_key in self.pending_starts:
                card.set_running(False)
                card.set_runtime(status="Pending")
            elif status:
                card.set_runtime(status=status)
        self.profile_summary.setText(
            f"{self.profile['name']} | TTC token "
            f"{self.core.preview_token(self.profile.get('auth_token', ''))} | "
            f"{len(self.accounts_by_key)} Facebook"
        )
        self._set_run_config_widgets(config)

    def _set_run_config_widgets(self, config):
        self._config_loading = True
        try:
            wanted = tuple(config["job_types"])
            index = next(
                (i for i in range(self.job_types_combo.count())
                 if set(self.job_types_combo.itemData(i)) == set(wanted)),
                2,
            )
            self.job_types_combo.setCurrentIndex(index)
            self.continuous_check.setChecked(config["continuous"])
            self.skip_hidden_check.setChecked(config["skip_hidden_comments"])
            self.max_jobs_spin.setValue(config["max_jobs_per_acc"])
            for key, spin in self.config_spins.items():
                spin.setValue(config[key])
        finally:
            self._config_loading = False
        self._config_dirty = False
        self.config_status_label.setText("")
        self.save_config_button.setText("Lưu cấu hình")

    def _mark_config_dirty(self, *_):
        if self._loading or self._config_loading:
            return
        self._config_dirty = True
        self.config_status_label.setText("Có thay đổi chưa lưu")
        self.save_config_button.setText("Lưu cấu hình")

    def _profile_changed(self):
        if self._loading:
            return
        profile_id = self.profile_combo.currentData()
        if not profile_id:
            return
        self._view_profile(profile_id)

    def create_profile(self):
        if not self._mutation_allowed():
            return
        dialog = ProfileDialog(self)
        if dialog.exec_() != QDialog.Accepted:
            return
        try:
            profile = self.core.create_profile_record(
                dialog.name.text().strip(), dialog.token.text().strip()
            )
            self.view_profile_id = str(profile["profile_id"])
            self.profile_id = self.view_profile_id
            self.reload_from_core()
        except Exception as exc:
            QMessageBox.critical(self, "Không thể tạo profile", str(exc))

    def rename_profile(self):
        if not self._mutation_allowed(require_profile=True):
            return
        name, accepted = QInputDialog.getText(
            self, "Đổi tên profile", "Tên mới:", text=self.profile["name"]
        )
        if accepted:
            try:
                self.core.rename_profile(self.profile_id, name)
                self.reload_from_core()
            except Exception as exc:
                QMessageBox.critical(self, "Không thể đổi tên", str(exc))

    def update_ttc_token(self):
        if not self._mutation_allowed(require_profile=True):
            return
        token, accepted = QInputDialog.getText(
            self, "Cập nhật TTC token", "TTC token:", QLineEdit.Password
        )
        if accepted:
            try:
                self.core.update_profile_auth_token_manual(self.profile_id, token)
                self.reload_from_core()
            except Exception as exc:
                QMessageBox.critical(self, "Không thể cập nhật token", str(exc))

    def delete_profile(self):
        if not self._mutation_allowed(require_profile=True):
            return
        answer = QMessageBox.question(
            self, "Xóa TTC profile",
            f"Xóa profile {self.profile['name']} và toàn bộ account thuộc profile?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        try:
            _deleted, active_id = self.core.delete_profile(self.profile_id)
            self.view_profile_id = str(active_id) if active_id is not None else None
            self.profile_id = self.view_profile_id
            self.reload_from_core()
        except Exception as exc:
            QMessageBox.critical(self, "Không thể xóa profile", str(exc))

    def copy_accounts(self):
        if not self._mutation_allowed(require_profile=True):
            return
        data = self.core.load_database()
        sources = [
            item for item in data.get("profiles", [])
            if item["profile_id"] != self.profile_id
        ]
        if not sources:
            QMessageBox.information(self, "Sao chép", "Không có profile nguồn khác.")
            return
        dialog = SelectAccountsDialog(sources, self)
        if dialog.exec_() != QDialog.Accepted:
            return
        source_profile_id, account_keys = dialog.selection()
        if not account_keys:
            QMessageBox.information(
                self, "Sao chép", "Hãy chọn ít nhất một Facebook account."
            )
            return
        try:
            result = self.core.copy_facebook_account_records(
                source_profile_id, self.profile_id, account_keys=account_keys
            )
            self.reload_from_core()
            QMessageBox.information(
                self, "Sao chép",
                f"Đã sao chép {result['copied']}; bỏ qua trùng {result['skipped']}.",
            )
        except Exception as exc:
            QMessageBox.critical(self, "Không thể sao chép", str(exc))

    def add_account(self):
        if not self._mutation_allowed(require_profile=True):
            return
        dialog = AddAccountDialog(self)
        if dialog.exec_() != QDialog.Accepted:
            return
        values = dialog.values()
        try:
            if values["mode"] == "manual":
                self.core.add_manual_account_record(self.profile_id, values["token"])
                self.reload_from_core()
            elif values["mode"] == "login":
                if not values["email"] or not values["password"]:
                    raise ValueError("UID/email và password không được để trống.")
                self._run_operation(
                    "Login Facebook",
                    lambda: self.core.add_login_account(
                        self.profile_id,
                        values["email"], values["password"], values["secret"],
                    ),
                    self._account_operation_finished,
                )
            else:
                self.core.parse_facebook_login_batch(values["batch"])
                self._run_operation(
                    "Batch login Facebook",
                    lambda: self.core.batch_login_accounts(
                        self.profile_id, values["batch"]
                    ),
                    self._account_operation_finished,
                )
        except Exception as exc:
            QMessageBox.critical(self, "Không thể thêm account", str(exc))

    def _account_operation_finished(self, result):
        if result is False:
            QMessageBox.warning(self, "Facebook", "Thao tác Facebook không thành công.")
        self.reload_from_core()

    def open_account_settings(self, account_key):
        if not self._mutation_allowed(require_profile=True):
            return
        account = self.accounts_by_key.get(account_key)
        if account is None:
            return
        enabled = account_key in set(self.core.selected_account_keys(self.profile))
        dialog = AccountSettingsDialog(account, enabled, self)
        if dialog.exec_() != QDialog.Accepted:
            return
        if dialog.action == "save":
            try:
                self.core.update_account_record(
                    self.profile_id,
                    account_key,
                    dialog.updates(),
                    enabled=dialog.enabled.isChecked(),
                )
                self.reload_from_core()
            except Exception as exc:
                QMessageBox.critical(self, "Không thể lưu account", str(exc))
        elif dialog.action == "relogin":
            password = dialog.password.text() or None
            secret = dialog.secret.text().strip() or None
            self._run_operation(
                "Relogin Facebook",
                lambda: self.core.relogin_account_record(
                    self.profile_id, account_key, password=password, secret=secret
                ),
                self._account_operation_finished,
            )

    def delete_account(self, account_key):
        if not self._mutation_allowed(require_profile=True):
            return
        account = self.accounts_by_key.get(account_key)
        if account is None:
            return
        answer = QMessageBox.question(
            self, "Xóa Facebook account", f"Xóa {_identity(account)}?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if answer == QMessageBox.Yes:
            try:
                self.core.delete_account_record(self.profile_id, account_key)
                self.reload_from_core()
            except Exception as exc:
                QMessageBox.critical(self, "Không thể xóa account", str(exc))

    def save_run_config(self):
        if not self._mutation_allowed(require_profile=True):
            return
        updates = {
            "job_types": list(self.job_types_combo.currentData()),
            "continuous": self.continuous_check.isChecked(),
            "max_jobs_per_acc": self.max_jobs_spin.value(),
            "skip_hidden_comments": self.skip_hidden_check.isChecked(),
        }
        updates.update({key: spin.value() for key, spin in self.config_spins.items()})
        self.config_status_label.setText("Đang lưu cấu hình...")
        self.save_config_button.setEnabled(False)
        QApplication.processEvents()
        try:
            current = self.core.normalize_run_config(self.profile.get("run_config"))
            expected = self.core.normalize_run_config({**current, **updates})
            saved = self.core.update_profile_run_config(self.profile_id, updates)
            self.reload_from_core()
            if self.profile is None:
                raise RuntimeError("Không thể tải lại TTC profile sau khi lưu cấu hình.")
            persisted = self.core.normalize_run_config(self.profile.get("run_config"))
            if persisted != saved or persisted != expected:
                raise RuntimeError("Cấu hình lưu lại không khớp với dữ liệu production core.")
            self._config_dirty = False
            self.config_status_label.setText("✓ Cấu hình đã được lưu vào dữ liệu core")
            self.save_config_button.setText("✓ Đã lưu")
            self.log("Đã lưu cấu hình chạy qua production core.")
        except Exception as exc:
            self._config_dirty = True
            self.config_status_label.setText("✕ Lưu cấu hình thất bại")
            self.save_config_button.setText("Lưu cấu hình")
            QMessageBox.critical(self, "Không thể lưu cấu hình", str(exc))
        finally:
            self.save_config_button.setEnabled(True)

    def check_balance(self):
        if not self._network_operation_allowed():
            return
        profile_id = self.view_profile_id
        self._run_operation(
            "Kiểm tra số dư",
            lambda: self.core.get_profile_balance(profile_id),
            lambda result: self._balance_finished(profile_id, result),
        )

    def _balance_finished(self, profile_id, result):
        balance = result.get("balance")
        if isinstance(balance, int) and not isinstance(balance, bool):
            self.profile_balances[profile_id] = balance
            self._update_profile_card(profile_id)
        QMessageBox.information(
            self, "Số dư TTC",
            f"TTC: {result.get('user', '')}\nSố dư: "
            f"{core.format_coin(balance) if balance is not None else '—'} xu",
        )

    def view_jobs(self):
        if not self._network_operation_allowed():
            return
        self._run_operation(
            "Xem jobs",
            lambda: self.core.get_profile_jobs_preview(self.profile_id),
            self._show_jobs_result,
        )

    def _show_jobs_result(self, result):
        jobs = result.get("jobs", [])
        lines = [f"UID: {result.get('uid')} | Tổng jobs: {len(jobs)}"]
        for index, wrapper in enumerate(jobs[:50], start=1):
            job = wrapper.get("_ttc_job", wrapper)
            lines.append(
                f"{index}. {job.get('action_group', job.get('type', 'unknown'))} | "
                f"{job.get('job_id', 'N/A')} | object {job.get('object_id', 'N/A')}"
            )
        QMessageBox.information(self, "Jobs TTC", "\n".join(lines))

    def _run_operation(self, label, operation, on_success):
        worker = CoreOperationWorker(operation, self)
        self._operations.add(worker)
        self._update_controls()
        self.log(f"{label} đang chạy nền…")
        worker.result_ready.connect(on_success)
        worker.result_ready.connect(lambda _result: self.log(f"{label} hoàn tất."))
        worker.error_ready.connect(
            lambda message: QMessageBox.critical(self, label, message)
        )
        worker.error_ready.connect(lambda message: self.log(f"{label}: {message}"))
        worker.finished.connect(lambda: self._operation_finished(worker))
        worker.start()

    def _operation_finished(self, worker):
        self._operations.discard(worker)
        worker.deleteLater()
        self._update_controls()
        if (self._closing and not self._operations
                and not self.workers and not self.pending_starts):
            QTimer.singleShot(0, self.close)

    def on_run_account(self, account_key):
        runtime_key = self._runtime_key(self.view_profile_id, account_key)
        worker = self.workers.get(runtime_key)
        if worker is not None:
            self.stop_account(account_key, profile_id=self.view_profile_id)
        else:
            self.start_account(account_key, profile_id=self.view_profile_id)

    def _ensure_profile_lease(self, profile_id):
        if self.profile_lease is not None:
            if self.run_profile_id != profile_id:
                running = self.profiles_by_id.get(self.run_profile_id, {})
                raise RuntimeError(
                    f"Profile {running.get('name', self.run_profile_id)} đang chạy. "
                    "Hãy dừng run-group hiện tại trước khi chạy profile khác."
                )
            return
        lease = self.core.ProfileRunLease(profile_id)
        lease.acquire()
        self.profile_lease = lease
        self.run_profile_id = profile_id
        profile = self.profiles_by_id.get(profile_id, {})
        self.log(
            f"Đã giữ profile run lease cho {profile.get('name', profile_id)}.",
            profile_id=profile_id,
        )

    def start_account(self, account_key, profile_id=None):
        profile_id = str(profile_id or self.view_profile_id or "")
        runtime_key = self._runtime_key(profile_id, account_key)
        if runtime_key in self.workers:
            return False
        if not profile_id:
            return False
        try:
            if self.run_profile_id is not None and self.run_profile_id != profile_id:
                running = self.profiles_by_id.get(self.run_profile_id, {})
                raise RuntimeError(
                    f"Profile {running.get('name', self.run_profile_id)} đang chạy. "
                    "Hãy dừng run-group hiện tại trước khi chạy profile khác."
                )
            profile = self.core.get_profile_snapshot(profile_id)
            config = self.core.normalize_run_config(profile.get("run_config"))
            account = next(
                (item for item in profile.get("fb_accounts", [])
                 if self.core.facebook_account_key(item) == account_key),
                None,
            )
            if account is None or not str(account.get("token", "")).strip():
                raise ValueError("Facebook account/token không còn hợp lệ.")
            if not str(profile.get("auth_token", "")).strip():
                raise ValueError("TTC profile chưa có token.")
            self._ensure_profile_lease(profile_id)
        except Exception as exc:
            QMessageBox.critical(self, "Không thể chạy account", str(exc))
            self._release_group_if_idle()
            return False

        self.pending_starts.discard(runtime_key)
        worker = JobWorker(
            profile_id,
            profile["auth_token"],
            account,
            config,
            runtime_factory=self.runtime_factory,
            parent=self,
        )
        worker.event_signal.connect(self._on_worker_event)
        worker.finished.connect(
            lambda pid=profile_id, key=account_key, current=worker:
            self._on_worker_finished(pid, key, current)
        )
        self.workers[runtime_key] = worker
        self.session_metrics[runtime_key] = {
            "jobs_succeeded": 0, "session_coin": 0, "balance": None
        }
        self.account_statuses[runtime_key] = "Running"
        self.profile_errors.discard(profile_id)
        card = self.account_widgets.get(account_key) if profile_id == self.view_profile_id else None
        if card is not None:
            card.set_running(True)
            card.set_runtime(jobs=0, coin=0)
        worker.start()
        self.log("Worker đã start.", account_key, profile_id)
        self._update_controls()
        return True

    def stop_account(self, account_key, profile_id=None):
        profile_id = str(profile_id or self.view_profile_id or "")
        runtime_key = self._runtime_key(profile_id, account_key)
        if runtime_key in self.pending_starts:
            self.pending_starts.discard(runtime_key)
            self._release_group_if_idle()
            self._update_controls()
            return
        worker = self.workers.get(runtime_key)
        if worker is None:
            return
        worker.request_stop()
        self.account_statuses[runtime_key] = "Stopping"
        card = self.account_widgets.get(account_key) if profile_id == self.view_profile_id else None
        if card is not None:
            card.set_running(True, stopping=True)
        self.log(
            "Đã gửi cooperative Stop; current job sẽ kết thúc an toàn.",
            account_key, profile_id,
        )
        self._update_profile_card(profile_id)

    def start_profile(self, profile_id):
        profile_id = str(profile_id or "").strip()
        if not profile_id or profile_id not in self.profiles_by_id:
            return False
        if self.run_profile_id is not None and self.run_profile_id != profile_id:
            running = self.profiles_by_id.get(self.run_profile_id, {})
            QMessageBox.warning(
                self, "Profile khác đang chạy",
                f"Profile {running.get('name', self.run_profile_id)} đang chạy. "
                "Hãy dừng run-group hiện tại trước khi chạy profile khác.",
            )
            return False
        try:
            profile = self.core.get_profile_snapshot(profile_id)
            config = self.core.normalize_run_config(profile.get("run_config"))
            accounts = self.core.selected_accounts(profile)
        except Exception as exc:
            QMessageBox.critical(self, "Không thể Start All", str(exc))
            return False
        if not accounts:
            QMessageBox.information(
                self,
                "Không có account để chạy",
                "Profile không có Facebook account nào đang bật để chạy.",
            )
            return False
        candidates = [
            item for item in accounts
            if self._runtime_key(profile_id, self.core.facebook_account_key(item))
            not in self.workers
            and self._runtime_key(profile_id, self.core.facebook_account_key(item))
            not in self.pending_starts
        ]
        if not candidates:
            return False
        self.log(
            "Đã yêu cầu chạy tất cả account đang bật.",
            profile_id=profile_id,
        )
        self._start_generation += 1
        generation = self._start_generation
        cumulative_delay = 0
        for index, account in enumerate(candidates):
            key = self.core.facebook_account_key(account)
            if index > 0:
                cumulative_delay += random.randint(
                    config["account_delay_min"], config["account_delay_max"]
                )
            if cumulative_delay <= 0:
                self.start_account(key, profile_id=profile_id)
                continue
            runtime_key = self._runtime_key(profile_id, key)
            self.pending_starts.add(runtime_key)
            QTimer.singleShot(
                cumulative_delay * 1000,
                lambda current_key=runtime_key, token=generation: self._launch_pending(
                    current_key, token
                ),
            )
        self._update_controls()
        return True

    def start_all(self):
        return self.start_profile(self.view_profile_id)

    def _launch_pending(self, runtime_key, generation):
        if generation != self._start_generation or runtime_key not in self.pending_starts:
            return
        self.pending_starts.discard(runtime_key)
        self.start_account(runtime_key[1], profile_id=runtime_key[0])
        self._release_group_if_idle()

    def stop_profile(self, profile_id):
        profile_id = str(profile_id or "").strip()
        if not profile_id or self.run_profile_id != profile_id:
            return False
        self._start_generation += 1
        pending_keys = [
            key for key in self.pending_starts if key[0] == profile_id
        ]
        for runtime_key in pending_keys:
            self.pending_starts.discard(runtime_key)
        active_keys = [key for key in self.workers if key[0] == profile_id]
        for _runtime_profile_id, account_key in active_keys:
            self.stop_account(account_key, profile_id=profile_id)
        self.log(
            "Đã yêu cầu dừng run-group profile.",
            profile_id=profile_id,
        )
        self._release_group_if_idle()
        self._update_controls()
        return True

    def stop_all(self):
        if self.run_profile_id is not None:
            return self.stop_profile(self.run_profile_id)
        self._start_generation += 1
        self.pending_starts.clear()
        for profile_id, account_key in list(self.workers):
            self.stop_account(account_key, profile_id=profile_id)
        self._release_group_if_idle()
        self._update_controls()
        return True

    def _on_worker_event(self, profile_id, account_key, event):
        runtime_key = self._runtime_key(profile_id, account_key)
        event_type = event.get("type")
        message = event.get("message", "")
        card = self.account_widgets.get(account_key) if profile_id == self.view_profile_id else None
        if event_type == "identity":
            self.resolved_uids[runtime_key] = event.get("uid")
            if card is not None:
                card.set_runtime(uid=event.get("uid"))
        if event_type in {"job_succeeded", "completion", "status", "balance", "stopped"}:
            metrics = self.session_metrics.setdefault(runtime_key, {})
            for field in ("jobs_succeeded", "session_coin", "balance"):
                if field in event and event[field] is not None:
                    metrics[field] = event[field]
            balance = metrics.get("balance")
            if isinstance(balance, int) and not isinstance(balance, bool):
                self.profile_balances[profile_id] = balance
            if card is not None:
                card.set_runtime(
                    status=event.get("status"),
                    jobs=metrics.get("jobs_succeeded", 0),
                    coin=metrics.get("session_coin", 0),
                )
        if event_type == "error":
            self.profile_errors.add(profile_id)
            self.account_statuses[runtime_key] = "Error"
            if card is not None:
                card.set_runtime(status="Error")
        elif event.get("status"):
            self.account_statuses[runtime_key] = event["status"]
        if message and event_type not in {"completion"}:
            self.log(message, account_key, profile_id)
        self._update_aggregate()
        self._update_profile_card(profile_id)

    def _on_worker_finished(self, profile_id, account_key, worker):
        runtime_key = self._runtime_key(profile_id, account_key)
        if self.workers.get(runtime_key) is not worker:
            return
        self.workers.pop(runtime_key, None)
        metrics = dict(self.session_metrics.pop(runtime_key, {}))
        if isinstance(worker.result, dict):
            metrics = dict(worker.result)
        self.account_last_metrics[runtime_key] = metrics
        completed = self.completed_profile_metrics.setdefault(
            profile_id, {"jobs_succeeded": 0, "session_coin": 0}
        )
        completed["jobs_succeeded"] += int(metrics.get("jobs_succeeded", 0))
        completed["session_coin"] += int(metrics.get("session_coin", 0))
        balance = metrics.get("balance")
        if isinstance(balance, int) and not isinstance(balance, bool):
            self.profile_balances[profile_id] = balance
        if worker.error is not None:
            status = "Error"
        elif worker.stop_event.is_set():
            status = "Stopped"
        else:
            status = "Completed"
        self.account_statuses[runtime_key] = status
        card = self.account_widgets.get(account_key) if profile_id == self.view_profile_id else None
        if card is not None:
            card.set_running(False)
            card.set_runtime(status=status)
            card.set_runtime(
                jobs=metrics.get("jobs_succeeded", 0),
                coin=metrics.get("session_coin", 0),
            )
        worker.deleteLater()
        self.log("Worker đã finished và cleanup.", account_key, profile_id)
        self._release_group_if_idle()
        self._update_controls()
        self._update_profile_card(profile_id)
        if self._closing and not self.workers and not self.pending_starts:
            QTimer.singleShot(0, self.close)

    def _release_group_if_idle(self):
        if self.workers or self.pending_starts or self.profile_lease is None:
            return
        profile_id = self.run_profile_id
        self.profile_lease.release()
        self.profile_lease = None
        self.run_profile_id = None
        self.log("Đã release profile run lease sau worker cuối.", profile_id=profile_id)

    def _update_aggregate(self):
        profile_id = self.view_profile_id
        jobs, coin = self._profile_totals(profile_id) if profile_id else (0, 0)
        active = sum(key[0] == profile_id for key in self.workers)
        pending = sum(key[0] == profile_id for key in self.pending_starts)
        self.aggregate_label.setText(
            f"Active: {active} | Pending: {pending} | "
            f"Tổng session: {jobs} job | +{core.format_coin(coin)} xu"
        )

    def _update_controls(self):
        active = bool(self.workers or self.pending_starts)
        busy = active or bool(self._operations)
        has_profile = self.view_profile_id is not None
        can_start_view = (
            has_profile and not self._operations
            and (self.run_profile_id is None or self.run_profile_id == self.view_profile_id)
        )
        self.stop_all_button.setEnabled(active)
        self.start_all_button.setEnabled(can_start_view)
        self.profile_combo.setEnabled(has_profile and not self._operations)
        self.refresh_button.setEnabled(not self._operations)
        self.overview_refresh_button.setEnabled(not self._operations)
        for widget in (
            self.new_profile_button,
            self.rename_profile_button,
            self.copy_accounts_button,
            self.delete_profile_button,
            self.add_account_button,
            self.update_ttc_token_button,
            self.save_config_button,
            self.balance_button,
            self.jobs_button,
            self.job_types_combo,
            self.continuous_check,
            self.skip_hidden_check,
            self.max_jobs_spin,
            *self.config_spins.values(),
        ):
            widget.setEnabled(not busy and (has_profile or widget is self.new_profile_button))
        for card in self.account_widgets.values():
            card.set_mutation_enabled(not busy)
            card.set_run_enabled(can_start_view)
        self._update_aggregate()
        self._update_dashboard()

    def _mutation_allowed(self, require_profile=False):
        if self.workers or self.pending_starts or self.profile_lease is not None:
            QMessageBox.warning(
                self, "Profile đang chạy",
                "Hãy dừng toàn bộ worker trước khi thay đổi profile/account/config.",
            )
            return False
        if self._operations:
            QMessageBox.warning(
                self, "Thao tác đang chạy",
                "Hãy chờ thao tác nền hiện tại hoàn tất trước khi thay đổi dữ liệu.",
            )
            return False
        if require_profile and self.profile_id is None:
            QMessageBox.warning(self, "Chưa có profile", "Hãy tạo TTC profile trước.")
            return False
        return True

    def _network_operation_allowed(self):
        if not self._mutation_allowed(require_profile=True):
            return False
        return True

    def closeEvent(self, event):
        if not self.workers and not self.pending_starts and not self._operations:
            self._release_group_if_idle()
            event.accept()
            return
        if not self._closing:
            answer = QMessageBox.question(
                self, "Thoát",
                "Worker/thao tác nền đang chạy. Gửi cooperative Stop và thoát "
                "sau khi tất cả thread finished?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                event.ignore()
                return
            self._closing = True
            self.stop_all()
        event.ignore()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())
