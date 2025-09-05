# nour_anfeh_gui.py
# Qt (PySide6) GUI for Nour Anfeh WhatsApp bills — with built-in Local API management
# Windows-ready:
#  - Detect Chrome/Edge path on Windows
#  - Use npm.cmd on Windows
#  - Hide Node console window on Windows
#  - Cleanup on exit uses taskkill (Windows) / pkill (macOS/Linux)
#  - Safe UI threading (no QMessageBox from worker threads)

import sys, os, platform, traceback, threading, subprocess, json, io
from pathlib import Path
import pandas as pd
from datetime import datetime

from PySide6.QtCore import Qt, QLocale, Signal, QObject, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QApplication, QWidget, QFileDialog, QMessageBox,
    QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QComboBox,
    QLineEdit, QTextEdit, QGroupBox, QGridLayout
)

import requests
import qrcode

# ================== Business config ==================
COMPANY_NAME = "نور أنفه"
COMPANY_PHONE = "81 215 712"
PAYMENT_DEADLINE_DAY = 7
CURRENCY_NOTE = "يمكن الدفع بالليرة اللبنانية حسب سعر الصرف في يوم الدفع."
DISPLAY_NAME_FIELD = "NameArabic"

MONTHS_AR = [
    ("01", "كانون الثاني"), ("02", "شباط"), ("03", "آذار"), ("04", "نيسان"),
    ("05", "أيار"), ("06", "حزيران"), ("07", "تموز"), ("08", "آب"),
    ("09", "أيلول"), ("10", "تشرين الأول"), ("11", "تشرين الثاني"), ("12", "كانون الأول")
]

# ===== Paths & Local API settings =====
APP_DIR = Path(__file__).resolve().parent

# PyInstaller-friendly resource resolver
def resource_path(rel_path: str) -> str:
    base = getattr(sys, "_MEIPASS", APP_DIR)
    return str(Path(base) / rel_path)

WA_API_DIR = Path(resource_path("wa_local_api"))   # folder that contains wa_http_server.js
WA_NODE_ENTRY = WA_API_DIR / "wa_http_server.js"
WA_PORT = 3000
WA_BASE = f"http://localhost:{WA_PORT}"

def default_browser_path() -> str:
    if platform.system() == "Windows":
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        ]
        for p in candidates:
            if os.path.exists(p):
                return p
        return r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    elif platform.system() == "Darwin":
        return "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    else:
        # Linux fallbacks
        for p in ["/usr/bin/google-chrome", "/usr/bin/chromium-browser", "/usr/bin/chromium", "/snap/bin/chromium"]:
            if os.path.exists(p): return p
        return "/usr/bin/google-chrome"

CHROME_PATH_DEFAULT = default_browser_path()

# ================== WhatsApp Service Manager ==================
class NodeLogPump(QObject):
    line = Signal(str)

class WhatsAppService(QObject):
    started = Signal()
    stopped = Signal()
    qr_changed = Signal(QPixmap)     # GUI shows QR here
    status_line = Signal(str)        # log lines

    def __init__(self):
        super().__init__()
        self.proc = None
        self._pump = NodeLogPump()

    def is_running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, headless=True):
        if self.is_running():
            self.status_line.emit("Service already running.")
            return

        if not WA_NODE_ENTRY.exists():
            self.status_line.emit(f"Server file not found: {WA_NODE_ENTRY}")
            return

        # Prepare environment for Node child process
        env = os.environ.copy()
        if not env.get("CHROME_PATH"):
            env["CHROME_PATH"] = CHROME_PATH_DEFAULT
        env["HEADLESS"] = "true" if headless else "false"
        # disable remote cache by default to avoid LocalWebCache crash
        env["WEB_CACHE_STRATEGY"] = env.get("WEB_CACHE_STRATEGY", "none")

        # If node_modules missing, try to run npm ci (silent)
        node_modules = WA_API_DIR / "node_modules"
        if not node_modules.exists():
            self.status_line.emit("Installing Node dependencies (first run)…")
            try:
                npm_cmd = "npm.cmd" if platform.system() == "Windows" else "npm"
                subprocess.run(
                    [npm_cmd, "ci"],
                    cwd=str(WA_API_DIR),
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE
                )
                self.status_line.emit("Dependencies installed.")
            except Exception as e:
                self.status_line.emit(f"npm install failed: {e}")
                return

        # Start Node server
        try:
            creationflags = 0x08000000 if platform.system() == "Windows" else 0  # CREATE_NO_WINDOW
            self.proc = subprocess.Popen(
                ["node", str(WA_NODE_ENTRY)],
                cwd=str(WA_API_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                text=True,
                bufsize=1,
                creationflags=creationflags
            )
        except Exception as e:
            self.status_line.emit(f"Failed to start server: {e}")
            return

        self.status_line.emit("Starting WhatsApp local API…")
        self.started.emit()

        # Pump logs
        t = threading.Thread(target=self._read_output, daemon=True)
        t.start()

        # Begin polling status/QR
        QTimer.singleShot(1500, self.poll_status)

    def _read_output(self):
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            if not line:
                break
            self.status_line.emit(line.rstrip())

    def stop(self):
        if not self.is_running():
            self.status_line.emit("Service not running.")
            return
        try:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except Exception:
                self.proc.kill()  # hard kill if it didn’t exit
        except Exception:
            pass
        self.proc = None
        self.stopped.emit()
        self.status_line.emit("Service stopped.")

    def poll_status(self):
        """Poll /status and /qr; emit QR pixmap if present."""
        if not self.is_running():
            return
        try:
            r = requests.get(f"{WA_BASE}/status", timeout=3)
            # Be tolerant to non-JSON when server is still booting
            try:
                data = r.json()
            except Exception:
                self.status_line.emit("Status: server not ready yet…")
                QTimer.singleShot(1500, self.poll_status)
                return

            if data.get("needQr"):
                # fetch QR text and render to image
                try:
                    q = requests.get(f"{WA_BASE}/qr", timeout=3).json()
                    if q.get("ok"):
                        pix = self._qr_to_pixmap(q.get("qr", ""))
                        if pix:
                            self.qr_changed.emit(pix)
                            self.status_line.emit("Awaiting QR scan…")
                    else:
                        self.qr_changed.emit(QPixmap())
                except Exception:
                    self.qr_changed.emit(QPixmap())
            else:
                # ready: clear QR if shown
                self.qr_changed.emit(QPixmap())
        except Exception as e:
            self.status_line.emit(f"Status error: {e}")
        # schedule next poll
        QTimer.singleShot(1500, self.poll_status)

    def _qr_to_pixmap(self, qr_text: str) -> QPixmap:
        try:
            img = qrcode.make(qr_text)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            pix = QPixmap()
            pix.loadFromData(buf.getvalue(), "PNG")
            return pix
        except Exception:
            return QPixmap()

# ================== GUI ==================
class MainWin(QWidget):
    # Signals to show message boxes on the main thread
    info_msg = Signal(str, str)   # title, text
    error_msg = Signal(str, str)  # title, text

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Nour Anfeh — WhatsApp Bills")
        self.setMinimumSize(980, 640)

        # Arabic layout
        self.setLayoutDirection(Qt.RightToLeft)
        QLocale.setDefault(QLocale(QLocale.Arabic))

        self.svc = WhatsAppService()
        self._build_ui()
        self.last_out_path = None

        # wire service signals
        self.svc.status_line.connect(self._log_service)
        self.svc.qr_changed.connect(self._set_qr)

        # connect message box signals to main-thread handlers
        self.info_msg.connect(lambda title, text: QMessageBox.information(self, title, text))
        self.error_msg.connect(lambda title, text: QMessageBox.critical(self, title, text))

    # -------- graceful exit: stop service + kill Chrome/Node --------
    def _kill_browsers_and_node(self):
        try:
            if platform.system() == "Windows":
                subprocess.run(['taskkill', '/F', '/IM', 'chrome.exe'], check=False,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.run(['taskkill', '/F', '/IM', 'msedge.exe'], check=False,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.run(['taskkill', '/F', '/IM', 'node.exe'], check=False,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                subprocess.run(['pkill', '-f', 'Google Chrome'], check=False,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.run(['pkill', '-f', 'wa_http_server.js'], check=False,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    def closeEvent(self, event):
        try:
            if self.svc.is_running():
                self.svc.stop()
        except Exception:
            pass
        self._kill_browsers_and_node()
        event.accept()

    def _build_ui(self):
        root = QVBoxLayout(self)

        # Header
        hdr = QLabel("✅ جاهز — برنامج إرسال فواتير نور أنفه")
        hdr.setStyleSheet("font-weight: 600; font-size: 16px;")
        root.addWidget(hdr)

        # ===== WhatsApp Service panel =====
        svc_group = QGroupBox("خدمة واتساب المحلية (تلقائية)")
        svc = QGridLayout(svc_group)

        self.btn_svc_start = QPushButton("تشغيل الخدمة")
        self.btn_svc_stop  = QPushButton("إيقاف الخدمة")
        self.btn_svc_head  = QPushButton("تشغيل الخدمة (مرئية)")
        self.btn_health    = QPushButton("فحص الحالة")
        self.svc_log       = QTextEdit(); self.svc_log.setReadOnly(True)
        self.qr_label      = QLabel(); self.qr_label.setFixedSize(220, 220); self.qr_label.setStyleSheet("background:#fafafa;border:1px solid #ccc;")

        self.btn_svc_start.clicked.connect(lambda: self.svc.start(headless=True))
        self.btn_svc_head.clicked.connect(lambda: self.svc.start(headless=False))
        self.btn_svc_stop.clicked.connect(self.svc.stop)
        self.btn_health.clicked.connect(self._check_health)

        svc.addWidget(self.btn_svc_start, 0, 0)
        svc.addWidget(self.btn_svc_head, 0, 1)
        svc.addWidget(self.btn_svc_stop, 0, 2)
        svc.addWidget(self.btn_health, 0, 3)
        svc.addWidget(QLabel("رمز QR (أول مرة فقط):"), 1, 0, 1, 2)
        svc.addWidget(self.qr_label, 2, 0, 2, 2)
        svc.addWidget(QLabel("سجلّ الخدمة:"), 1, 2, 1, 2)
        svc.addWidget(self.svc_log, 2, 2, 2, 2)
        root.addWidget(svc_group)

        # ===== Month + price inputs =====
        month_row = QHBoxLayout()
        month_row.addWidget(QLabel("اختر الشهر:"))
        self.month_combo = QComboBox()
        for num, name in MONTHS_AR:
            self.month_combo.addItem(f"{num} - {name}", userData=num)
        cur_m = datetime.now().strftime("%m")
        try: self.month_combo.setCurrentIndex([i for i,(n,_) in enumerate(MONTHS_AR) if n==cur_m][0])
        except: self.month_combo.setCurrentIndex(0)
        month_row.addWidget(self.month_combo, 1)
        root.addLayout(month_row)

        price_row = QHBoxLayout()
        price_row.addWidget(QLabel("سعر الكيلوواط (USD):"))
        self.price_kwh = QLineEdit(); self.price_kwh.setPlaceholderText("مثال: 0.38")
        price_row.addWidget(self.price_kwh, 1)
        root.addLayout(price_row)

        # ===== Input files =====
        inputs = QGroupBox("ملفات الإدخال")
        in_lay = QVBoxLayout(inputs)

        # Customers
        cust_row = QHBoxLayout()
        cust_row.addWidget(QLabel("customers_master.csv (دائم):"))
        self.cust_path = QLineEdit(); self.cust_path.setPlaceholderText("اختيار ملف العملاء...")
        b1 = QPushButton("تصفّح"); b1.clicked.connect(self.pick_customers)
        cust_row.addWidget(self.cust_path, 1); cust_row.addWidget(b1); in_lay.addLayout(cust_row)

        # Subscription fees
        subs_row = QHBoxLayout()
        subs_row.addWidget(QLabel("subscriptions_prices.csv (أنواع الاشتراك وأسعارها):"))
        self.subs_path = QLineEdit(); self.subs_path.setPlaceholderText("اختيار ملف أسعار الاشتراكات...")
        b2 = QPushButton("تصفّح"); b2.clicked.connect(self.pick_subs)
        subs_row.addWidget(self.subs_path, 1); subs_row.addWidget(b2); in_lay.addLayout(subs_row)

        # Readings
        read_row = QHBoxLayout()
        read_row.addWidget(QLabel("meter_readings_YYYY_MM.csv (شهري):"))
        self.read_path = QLineEdit(); self.read_path.setPlaceholderText("اختيار ملف قراءات العدّاد...")
        b3 = QPushButton("تصفّح"); b3.clicked.connect(self.pick_readings)
        read_row.addWidget(self.read_path, 1); read_row.addWidget(b3); in_lay.addLayout(read_row)

        root.addWidget(inputs)

        # Hint
        self.out_hint = QLabel("سيتم حفظ messages_preview.csv بجانب ملف القراءات.\nتنبيه: ملف الأسعار يجب أن يحتوي الأعمدة: SubscriptionType, SubscriptionFeeUSD.")
        self.out_hint.setStyleSheet("color:#555;")
        root.addWidget(self.out_hint)

        # ===== Output log =====
        out_group = QGroupBox("السجلّ")
        out_lay = QVBoxLayout(out_group)
        self.log = QTextEdit(); self.log.setReadOnly(True)
        out_lay.addWidget(self.log)
        root.addWidget(out_group, 1)

        # ===== Buttons =====
        btn_row = QHBoxLayout()
        self.btn_run = QPushButton("تحضير الرسائل (CSV)")
        self.btn_send_api = QPushButton("إرسال عبر الخدمة المحلية (API)")
        self.btn_run.clicked.connect(self.run)
        self.btn_send_api.clicked.connect(self.send_via_local_api)
        btn_row.addWidget(self.btn_run)
        btn_row.addWidget(self.btn_send_api)
        btn_row.addStretch(1)
        exit_btn = QPushButton("خروج"); exit_btn.clicked.connect(self.close)
        btn_row.addWidget(exit_btn)
        root.addLayout(btn_row)

    # ===== Helpers =====
    def _set_qr(self, pix: QPixmap):
        if pix.isNull():
            self.qr_label.clear()
            self.qr_label.setStyleSheet("background:#fafafa;border:1px solid #ccc;")
        else:
            self.qr_label.setPixmap(pix.scaled(self.qr_label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def _log_service(self, line: str):
        self.svc_log.append(line)

    def _check_health(self):
        try:
            r = requests.get(f"{WA_BASE}/health", timeout=3)
            try:
                self.svc_log.append(json.dumps(r.json(), ensure_ascii=False))
            except Exception:
                self.svc_log.append("Health: server not ready yet…")
        except Exception as e:
            self.svc_log.append(f"Health error: {e}")

    def pick_customers(self):
        path, _ = QFileDialog.getOpenFileName(self, "اختر customers_master.csv", "", "CSV (*.csv);;All Files (*)")
        if path: self.cust_path.setText(path)

    def pick_subs(self):
        path, _ = QFileDialog.getOpenFileName(self, "اختر subscriptions_prices.csv", "", "CSV (*.csv);;All Files (*)")
        if path: self.subs_path.setText(path)

    def pick_readings(self):
        path, _ = QFileDialog.getOpenFileName(self, "اختر meter_readings_YYYY_MM.csv", "", "CSV (*.csv);;All Files (*)")
        if path: self.read_path.setText(path)

    def log_line(self, text: str):
        self.log.append(text)

    # ===== Core: prepare CSV =====
    def run(self):
        try:
            pkwh_text = (self.price_kwh.text() or "").strip()
            try:
                price_per_kwh = float(pkwh_text)
            except:
                return self._err("الرجاء إدخال سعر الكيلوواط بالدولار (مثال: 0.38)")
            if price_per_kwh < 0:
                return self._err("سعر الكيلوواط يجب أن يكون رقماً موجباً")

            customers_csv = self.cust_path.text().strip()
            readings_csv  = self.read_path.text().strip()
            subs_csv      = self.subs_path.text().strip()
            if not customers_csv or not os.path.isfile(customers_csv): return self._err("الرجاء اختيار ملف customers_master.csv")
            if not subs_csv or not os.path.isfile(subs_csv): return self._err("الرجاء اختيار ملف subscriptions_prices.csv")
            if not readings_csv or not os.path.isfile(readings_csv): return self._err("الرجاء اختيار ملف meter_readings_YYYY_MM.csv")

            self.log_line(f"قراءة: {customers_csv}")
            customers = pd.read_csv(customers_csv, dtype=str)

            self.log_line(f"قراءة: {readings_csv}")
            readings = pd.read_csv(readings_csv, dtype=str)

            self.log_line(f"قراءة: {subs_csv}")
            subs = pd.read_csv(subs_csv, dtype=str)
            subs_cols = {c.strip(): c for c in subs.columns}
            for nc in ["SubscriptionType", "SubscriptionFeeUSD"]:
                if nc not in subs_cols: return self._err("ملف الأسعار يجب أن يحتوي الأعمدة: SubscriptionType, SubscriptionFeeUSD")
            subs = subs[[subs_cols["SubscriptionType"], subs_cols["SubscriptionFeeUSD"]]].rename(columns={
                subs_cols["SubscriptionType"]: "SubscriptionType",
                subs_cols["SubscriptionFeeUSD"]: "SubscriptionFeeUSD"
            })
            subs["SubscriptionFeeUSD"] = pd.to_numeric(subs["SubscriptionFeeUSD"], errors="coerce")

            for col in ["PrevKWh", "CurrKWh"]:
                if col in readings.columns:
                    readings[col] = pd.to_numeric(readings[col], errors="coerce")

            df = readings.merge(customers, on="CustomerID", how="left")
            df = df.merge(subs, on="SubscriptionType", how="left")

            df["Status"] = ""
            df.loc[df["NameArabic"].isna() | df["Phone"].isna(), "Status"] = "MISSING_CONTACT"
            df.loc[df["PrevKWh"].isna() | df["CurrKWh"].isna(), "Status"] = df["Status"].where(df["Status"] != "", "MISSING_READING")

            df["UsageKWh"] = df["CurrKWh"] - df["PrevKWh"]
            df.loc[df["UsageKWh"] < 0, "Status"] = df["Status"].where(df["Status"] != "", "ERROR_READING_DECREASED")

            if "SubscriptionFeeUSD" in df.columns:
                df["MonthlyFeeUSD"] = df["SubscriptionFeeUSD"].round(2)
            else:
                df["MonthlyFeeUSD"] = 0.0
            df["PricePerKWh"] = price_per_kwh
            df.loc[df["MonthlyFeeUSD"].isna(), "Status"] = df["Status"].where(df["Status"] != "", "MISSING_SUBS_FEE")
            df["MonthlyFeeUSD"] = df["MonthlyFeeUSD"].fillna(0)
            df.loc[df["MonthlyFeeUSD"] == 0, "Status"] = df["Status"].where(df["Status"] != "", "MISSING_SUBS_FEE")

            ok_mask = df["Status"] == ""
            df.loc[ok_mask, "EnergyUSD"] = (df.loc[ok_mask, "UsageKWh"] * df.loc[ok_mask, "PricePerKWh"]).round(2)
            df.loc[ok_mask, "TotalUSD"] = (df.loc[ok_mask, "EnergyUSD"] + df.loc[ok_mask, "MonthlyFeeUSD"]).round(2)

            def build_msg(r):
                if r["Status"] != "": return ""
                name = r.get(DISPLAY_NAME_FIELD, "")
                prev_kwh = r.get("PrevKWh", ""); curr_kwh = r.get("CurrKWh", ""); usage = r.get("UsageKWh", "")
                sub = r.get("SubscriptionType", ""); fee = r.get("MonthlyFeeUSD", ""); ppk = r.get("PricePerKWh", "")
                energy = r.get("EnergyUSD", ""); total = r.get("TotalUSD", "")
                month_name = dict(MONTHS_AR).get(self.month_combo.currentData(),'—')
                return (
                    f"مرحباً {name}،\n"
                    f"فاتورة {COMPANY_NAME} لشهر {month_name}:\n"
                    f"الاستهلاك: {usage} ك.و.س (السابق {prev_kwh}، الحالي {curr_kwh}).\n"
                    f"الاشتراك: {sub} أمبير — رسم شهري {fee}$\n"
                    f"سعر الكيلوواط: {ppk}$ ⇒ قيمة الطاقة: {energy}$\n"
                    f"الإجمالي: {total}$\n"
                    f"{CURRENCY_NOTE}\n"
                    f"يرجى التسديد قبل يوم {PAYMENT_DEADLINE_DAY} من الشهر. للاستفسار: {COMPANY_PHONE}"
                )

            df["MessageArabic"] = df.apply(build_msg, axis=1)

            base_cols = ["CustomerID", DISPLAY_NAME_FIELD, "Phone", "SubscriptionType",
                         "PrevKWh", "CurrKWh", "UsageKWh", "PricePerKWh",
                         "SubscriptionFeeUSD", "MonthlyFeeUSD",
                         "EnergyUSD", "TotalUSD", "Status", "MessageArabic"]
            out = df[base_cols].rename(columns={DISPLAY_NAME_FIELD: "DisplayName"})
            out_dir = os.path.dirname(readings_csv) or os.getcwd()
            out_path = os.path.join(out_dir, "messages_preview.csv")
            out.to_csv(out_path, index=False, encoding="utf-8-sig")
            self.last_out_path = out_path

            total_rows = len(out); ok_rows = int((out["Status"] == "").sum()); err_rows = total_rows - ok_rows
            self.log_line(f"تم التحضير بنجاح: {out_path}")
            self.log_line(f"عدد السجلات: {total_rows} (جاهزة للإرسال: {ok_rows}، بها مشاكل: {err_rows})")
            self.info_msg.emit("نجاح", f"تم إنشاء الملف:\n{out_path}\n\nجاهزة للإرسال: {ok_rows}\nبها مشاكل: {err_rows}")
        except Exception as e:
            tb = traceback.format_exc(); self._err(f"{e}\n\n{tb}")

    # ===== Send via Local API =====
    def send_via_local_api(self):
        try:
            if not self.svc.is_running():
                return self._err("الرجاء تشغيل خدمة واتساب المحلية أولاً (زر تشغيل الخدمة).")

            csv_path = self.last_out_path
            if not csv_path or not os.path.isfile(csv_path):
                path, _ = QFileDialog.getOpenFileName(self, "اختر ملف messages_preview.csv", "", "CSV (*.csv);;All Files (*)")
                if not path: return
                csv_path = path

            df = pd.read_csv(csv_path, dtype=str)
            df = df[df["Status"].fillna("") == ""].copy()
            if df.empty:
                return self._err("لا توجد سجلات جاهزة (Status يجب أن يكون فارغ).")

            def norm_phone(s):
                s = "".join(ch for ch in str(s) if ch.isdigit())
                return s[2:] if s.startswith("00") else s

            items = []
            for _, r in df.iterrows():
                phone = norm_phone(r.get("Phone",""))
                msg = (r.get("MessageArabic","") or "").strip()
                if phone and msg: items.append({"phone": phone, "message": msg})

            self.log_line(f"بدء الإرسال عبر الخدمة المحلية… ({len(items)} رسالة)")
            t = threading.Thread(target=self._post_bulk, args=(items,), daemon=True)
            t.start()
        except Exception as e:
            tb = traceback.format_exc(); self._err(f"{e}\n\n{tb}")

    def _post_bulk(self, items):
        try:
            r = requests.post(f"{WA_BASE}/bulk", json={"items": items}, timeout=max(60, len(items)//2))
            data = r.json()
            ok = sum(1 for x in data.get("results", []) if x.get("ok"))
            fail = sum(1 for x in data.get("results", []) if not x.get("ok"))
            self.log_line(f"انتهى الإرسال: ناجحة={ok}، فاشلة={fail}")
            self.info_msg.emit("انتهى", f"تم الإرسال.\nنجاح: {ok}\nفشل: {fail}")
        except Exception as e:
            self._err(f"فشل الاتصال بالخدمة: {e}")

    def _err(self, msg: str):
        self.log_line(f"خطأ: {msg}")
        self.error_msg.emit("خطأ", msg)

# ============= main =============
def main():
    app = QApplication(sys.argv)
    win = MainWin()
    win.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()