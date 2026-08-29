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

    event_signal = pyqtSignal(str, object)

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
        self.event_signal.emit(self.account_key, dict(event))

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
        self.profile_id = None
        self.profile = None
        self.accounts_by_key = {}
        self.account_widgets = {}
        self.session_metrics = {}
        self.resolved_uids = {}
        self._operations = set()
        self._loading = False
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
        profile_row = QHBoxLayout()
        profile_row.addWidget(QLabel("TTC profile:"))
        self.profile_combo = QComboBox()
        self.profile_combo.currentIndexChanged.connect(self._profile_changed)
        profile_row.addWidget(self.profile_combo, 1)
        self.new_profile_button = QPushButton("＋ Tạo")
        self.rename_profile_button = QPushButton("Đổi tên")
        self.copy_accounts_button = QPushButton("Sao chép FB")
        self.delete_profile_button = QPushButton("Xóa profile")
        self.delete_profile_button.setObjectName("dangerButton")
        self.refresh_button = QPushButton("↻")
        self.new_profile_button.clicked.connect(self.create_profile)
        self.rename_profile_button.clicked.connect(self.rename_profile)
        self.copy_accounts_button.clicked.connect(self.copy_accounts)
        self.delete_profile_button.clicked.connect(self.delete_profile)
        self.refresh_button.clicked.connect(self.reload_from_core)
        for button in (
            self.new_profile_button,
            self.rename_profile_button,
            self.copy_accounts_button,
            self.delete_profile_button,
            self.refresh_button,
        ):
            profile_row.addWidget(button)
        accounts_layout.addLayout(profile_row)

        add_row = QHBoxLayout()
        self.add_account_button = QPushButton("＋ Thêm Facebook")
        self.add_account_button.clicked.connect(self.add_account)
        self.aggregate_label = QLabel("Active: 0 | Tổng session: 0 job")
        add_row.addWidget(self.add_account_button)
        add_row.addStretch()
        add_row.addWidget(self.aggregate_label)
        accounts_layout.addLayout(add_row)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.cards_container = QWidget()
        self.cards_layout = QVBoxLayout(self.cards_container)
        self.cards_layout.setAlignment(Qt.AlignTop)
        scroll.setWidget(self.cards_container)
        accounts_layout.addWidget(scroll, 1)

        run_row = QHBoxLayout()
        self.start_all_button = QPushButton("▶ Chạy tất cả đã bật")
        self.stop_all_button = QPushButton("⏹ Dừng tất cả")
        self.start_all_button.clicked.connect(self.start_all)
        self.stop_all_button.clicked.connect(self.stop_all)
        run_row.addWidget(self.start_all_button)
        run_row.addWidget(self.stop_all_button)
        run_row.addStretch()
        accounts_layout.addLayout(run_row)
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

        config_group = QGroupBox("Cấu hình chạy production core")
        config_grid = QGridLayout(config_group)
        self.job_types_combo = QComboBox()
        self.job_types_combo.addItem("Like", ("like_facebook",))
        self.job_types_combo.addItem("Comment", ("like_poster",))
        self.job_types_combo.addItem("Both", ("like_facebook", "like_poster"))
        self.continuous_check = QCheckBox("Chạy liên tục")
        self.skip_hidden_check = QCheckBox("Bỏ comment bị ẩn")
        self.max_jobs_spin = self._spin(1, 100000)
        config_grid.addWidget(QLabel("Loại job"), 0, 0)
        config_grid.addWidget(self.job_types_combo, 0, 1)
        config_grid.addWidget(QLabel("Max job/account"), 0, 2)
        config_grid.addWidget(self.max_jobs_spin, 0, 3)
        config_grid.addWidget(self.continuous_check, 1, 0, 1, 2)
        config_grid.addWidget(self.skip_hidden_check, 1, 2, 1, 2)

        self.config_spins = {}
        rows = (
            ("like_batch", "Burst Like", 1),
            ("comment_batch", "Burst Comment", 1),
            ("like_delay", "Delay Like (s)", 0),
            ("comment_delay", "Delay Comment (s)", 0),
            ("failed_delay", "Delay lỗi (s)", 0),
            ("account_delay", "Stagger account (s)", 0),
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
        config_grid.addWidget(self.save_config_button, len(rows) + 2, 0, 1, 4)
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

    def log(self, message, account_key=None):
        timestamp = datetime.now().strftime("%H:%M:%S")
        prefix = ""
        if account_key:
            account = self.accounts_by_key.get(account_key, {})
            prefix = f"[{_identity(account)}] "
        self.log_text.append(f"[{timestamp}] {prefix}{message}")

    def reload_from_core(self):
        if self.workers or self.pending_starts:
            self.log("Không reload dữ liệu khi run-group đang active.")
            return
        try:
            data = self.core.load_database()
        except Exception as exc:
            QMessageBox.critical(self, "Lỗi dữ liệu", str(exc))
            return
        self._loading = True
        try:
            previous_id = self.profile_id
            profiles = data.get("profiles", [])
            valid_ids = {item["profile_id"] for item in profiles}
            selected_id = previous_id if previous_id in valid_ids else data.get("active_profile_id")
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
            self.profile_id = selected_id if selected_id in valid_ids else None
            self.profile = self.core.get_profile(data, self.profile_id)
            self._render_profile()
        finally:
            self._loading = False
        self._update_controls()

    def _render_profile(self):
        while self.cards_layout.count():
            item = self.cards_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.account_widgets = {}
        self.accounts_by_key = {}

        if self.profile is None:
            self.profile_summary.setText("Chưa có TTC profile")
            self._set_run_config_widgets(self.core.normalize_run_config())
            return
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
            metrics = self.session_metrics.get(key, {})
            card.set_runtime(
                uid=self.resolved_uids.get(key),
                jobs=metrics.get("jobs_succeeded", 0),
                coin=metrics.get("session_coin", 0),
            )
        self.profile_summary.setText(
            f"{self.profile['name']} | TTC token "
            f"{self.core.preview_token(self.profile.get('auth_token', ''))} | "
            f"{len(self.accounts_by_key)} Facebook"
        )
        self._set_run_config_widgets(config)

    def _set_run_config_widgets(self, config):
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

    def _profile_changed(self):
        if self._loading or self.workers or self.pending_starts:
            return
        profile_id = self.profile_combo.currentData()
        if not profile_id:
            return
        try:
            self.core.select_profile_as_default(profile_id)
            self.profile_id = profile_id
            self.reload_from_core()
        except Exception as exc:
            QMessageBox.critical(self, "Không thể chọn profile", str(exc))

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
            self.profile_id = profile["profile_id"]
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
            self.profile_id = active_id
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
        labels = [f"{item['name']} — {len(item.get('fb_accounts', []))} Facebook" for item in sources]
        label, accepted = QInputDialog.getItem(
            self, "Sao chép Facebook", "Profile nguồn:", labels, 0, False
        )
        if not accepted:
            return
        source = sources[labels.index(label)]
        try:
            result = self.core.copy_facebook_account_records(
                source["profile_id"], self.profile_id
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
        try:
            self.core.update_profile_run_config(self.profile_id, updates)
            self.reload_from_core()
            self.log("Đã lưu cấu hình chạy qua production core.")
        except Exception as exc:
            QMessageBox.critical(self, "Cấu hình không hợp lệ", str(exc))

    def check_balance(self):
        if not self._network_operation_allowed():
            return
        self._run_operation(
            "Kiểm tra số dư",
            lambda: self.core.get_profile_balance(self.profile_id),
            lambda result: QMessageBox.information(
                self, "Số dư TTC",
                f"TTC: {result.get('user', '')}\nSố dư: {result.get('balance')} xu",
            ),
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
        worker = self.workers.get(account_key)
        if worker is not None:
            self.stop_account(account_key)
        else:
            self.start_account(account_key)

    def _ensure_profile_lease(self):
        if self.profile_lease is not None:
            if self.profile_lease.profile_id != self.profile_id:
                raise RuntimeError("Run-group đang giữ lease của profile khác.")
            return
        lease = self.core.ProfileRunLease(self.profile_id)
        lease.acquire()
        self.profile_lease = lease
        self.log(f"Đã giữ profile run lease cho {self.profile['name']}.")

    def start_account(self, account_key):
        if account_key in self.workers:
            return False
        if self.profile_id is None:
            return False
        try:
            profile = self.core.get_profile_snapshot(self.profile_id)
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
            self._ensure_profile_lease()
        except Exception as exc:
            QMessageBox.critical(self, "Không thể chạy account", str(exc))
            self._release_group_if_idle()
            return False

        self.pending_starts.discard(account_key)
        worker = JobWorker(
            self.profile_id,
            profile["auth_token"],
            account,
            config,
            runtime_factory=self.runtime_factory,
            parent=self,
        )
        worker.event_signal.connect(self._on_worker_event)
        worker.finished.connect(
            lambda key=account_key, current=worker: self._on_worker_finished(key, current)
        )
        self.workers[account_key] = worker
        self.session_metrics[account_key] = {
            "jobs_succeeded": 0, "session_coin": 0, "balance": None
        }
        card = self.account_widgets.get(account_key)
        if card is not None:
            card.set_running(True)
            card.set_runtime(jobs=0, coin=0)
        worker.start()
        self.log("Worker đã start.", account_key)
        self._update_controls()
        return True

    def stop_account(self, account_key):
        if account_key in self.pending_starts:
            self.pending_starts.discard(account_key)
            self._release_group_if_idle()
            self._update_controls()
            return
        worker = self.workers.get(account_key)
        if worker is None:
            return
        worker.request_stop()
        card = self.account_widgets.get(account_key)
        if card is not None:
            card.set_running(True, stopping=True)
        self.log("Đã gửi cooperative Stop; current job sẽ kết thúc an toàn.", account_key)

    def start_all(self):
        if self.profile_id is None:
            return
        try:
            profile = self.core.get_profile_snapshot(self.profile_id)
            config = self.core.normalize_run_config(profile.get("run_config"))
            accounts = self.core.selected_accounts(profile)
        except Exception as exc:
            QMessageBox.critical(self, "Không thể Start All", str(exc))
            return
        candidates = [
            item for item in accounts
            if self.core.facebook_account_key(item) not in self.workers
            and self.core.facebook_account_key(item) not in self.pending_starts
        ]
        if not candidates:
            return
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
                self.start_account(key)
                continue
            self.pending_starts.add(key)
            QTimer.singleShot(
                cumulative_delay * 1000,
                lambda current_key=key, token=generation: self._launch_pending(
                    current_key, token
                ),
            )
        self._update_controls()

    def _launch_pending(self, account_key, generation):
        if generation != self._start_generation or account_key not in self.pending_starts:
            return
        self.pending_starts.discard(account_key)
        self.start_account(account_key)
        self._release_group_if_idle()

    def stop_all(self):
        self._start_generation += 1
        self.pending_starts.clear()
        for account_key in list(self.workers):
            self.stop_account(account_key)
        self._release_group_if_idle()
        self._update_controls()

    def _on_worker_event(self, account_key, event):
        event_type = event.get("type")
        message = event.get("message", "")
        card = self.account_widgets.get(account_key)
        if event_type == "identity":
            self.resolved_uids[account_key] = event.get("uid")
            if card is not None:
                card.set_runtime(uid=event.get("uid"))
        if event_type in {"job_succeeded", "completion", "status"}:
            metrics = self.session_metrics.setdefault(account_key, {})
            for field in ("jobs_succeeded", "session_coin", "balance"):
                if field in event and event[field] is not None:
                    metrics[field] = event[field]
            if card is not None:
                card.set_runtime(
                    status=event.get("status"),
                    jobs=metrics.get("jobs_succeeded", 0),
                    coin=metrics.get("session_coin", 0),
                )
        if message and event_type not in {"completion"}:
            self.log(message, account_key)
        self._update_aggregate()

    def _on_worker_finished(self, account_key, worker):
        if self.workers.get(account_key) is not worker:
            return
        self.workers.pop(account_key, None)
        if isinstance(worker.result, dict):
            self.session_metrics[account_key] = dict(worker.result)
        card = self.account_widgets.get(account_key)
        if card is not None:
            card.set_running(False)
            if worker.error is not None:
                card.set_runtime(status="Error")
            elif worker.stop_event.is_set():
                card.set_runtime(status="Stopped")
            else:
                card.set_runtime(status="Completed")
            metrics = self.session_metrics.get(account_key, {})
            card.set_runtime(
                jobs=metrics.get("jobs_succeeded", 0),
                coin=metrics.get("session_coin", 0),
            )
        worker.deleteLater()
        self.log("Worker đã finished và cleanup.", account_key)
        self._release_group_if_idle()
        self._update_controls()
        if self._closing and not self.workers and not self.pending_starts:
            QTimer.singleShot(0, self.close)

    def _release_group_if_idle(self):
        if self.workers or self.pending_starts or self.profile_lease is None:
            return
        self.profile_lease.release()
        self.profile_lease = None
        self.log("Đã release profile run lease sau worker cuối.")

    def _update_aggregate(self):
        jobs = sum(
            int(metrics.get("jobs_succeeded", 0))
            for metrics in self.session_metrics.values()
        )
        self.aggregate_label.setText(
            f"Active: {len(self.workers)} | Pending: {len(self.pending_starts)} | "
            f"Tổng session: {jobs} job"
        )

    def _update_controls(self):
        active = bool(self.workers or self.pending_starts)
        busy = active or bool(self._operations)
        has_profile = self.profile_id is not None
        self.stop_all_button.setEnabled(active)
        self.start_all_button.setEnabled(has_profile and not self._operations)
        for widget in (
            self.profile_combo,
            self.new_profile_button,
            self.rename_profile_button,
            self.copy_accounts_button,
            self.delete_profile_button,
            self.refresh_button,
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
        self._update_aggregate()

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
