# app.py
# ===== Qt plugin path hard-fix (must come before ANY PyQt/matplotlib import) =====
import os, sys, site, glob

def _fix_qt_plugin_path():
    # 既存の誤った環境変数を掃除（存在しないパスならクリア）
    for k in ("QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH"):
        v = os.environ.get(k)
        if v and not os.path.isdir(v):
            os.environ.pop(k, None)

    qt_base = None
    try:
        import PyQt5  # ここではまだQtCore等はimportしない
        qt_base = os.path.join(os.path.dirname(PyQt5.__file__), "Qt")
    except Exception:
        pass

    if not qt_base or not os.path.isdir(qt_base):
        # site-packages を総当たりで探索（venvが変わっても拾える）
        for sp in list(dict.fromkeys(site.getsitepackages() + [site.getusersitepackages()])):
            cand = glob.glob(os.path.join(sp, "PyQt5", "Qt"))
            if cand:
                qt_base = cand[0]
                break

    if qt_base and os.path.isdir(qt_base):
        plugins   = os.path.join(qt_base, "plugins")
        platforms = os.path.join(plugins, "platforms")
        bin_dir   = os.path.join(qt_base, "bin")

        os.environ["QT_QPA_PLATFORM"] = "windows"
        os.environ["QT_PLUGIN_PATH"] = plugins
        os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = platforms

        # DLL探索パスを追加（Py3.8+）
        if hasattr(os, "add_dll_directory"):
            for p in (bin_dir, plugins, platforms):
                if os.path.isdir(p):
                    os.add_dll_directory(p)

_fix_qt_plugin_path()
# ================================================================================



# === crash logging (must be very early) ===
import faulthandler, time

log_path = os.path.join(os.getcwd(), "crash_dump.txt")
# 追記モード＋逐次flush
_crash_log = open(log_path, "a", buffering=1, encoding="utf-8")
_crash_log.write(f"\n[faulthandler enabled {time.strftime('%Y-%m-%d %H:%M:%S')}]\n")

faulthandler.enable(_crash_log)


# 本体
import threading
from PyQt5 import QtWidgets, QtCore
from pipeline import init_model, run_zproject, run_cellcount, add_gui_log_sink, log_info
import time

QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling, True)

def _qt_msg(mode, ctx, msg):
    with open("qt_messages.log", "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%H:%M:%S')} | {msg}\n")
QtCore.qInstallMessageHandler(_qt_msg)


class ZProjTab(QtWidgets.QWidget):
    log_sig = QtCore.pyqtSignal(str)  # ★ 受信用
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QtWidgets.QFormLayout(self)

        self.in_edit  = QtWidgets.QLineEdit()
        self.out_edit = QtWidgets.QLineEdit()
        self.btn_in   = QtWidgets.QPushButton("選択")
        self.btn_out  = QtWidgets.QPushButton("選択")
        self.method   = QtWidgets.QComboBox()
        self.method.addItems(["max","average","median"])
        self.run_btn  = QtWidgets.QPushButton("Z-Project 実行")
        self.log      = QtWidgets.QPlainTextEdit(); self.log.setReadOnly(True)

        h1 = QtWidgets.QHBoxLayout(); h1.addWidget(self.in_edit); h1.addWidget(self.btn_in)
        h2 = QtWidgets.QHBoxLayout(); h2.addWidget(self.out_edit); h2.addWidget(self.btn_out)

        lay.addRow("入力ルート（スタック群の親）", h1)
        lay.addRow("出力ルート（投影画像）", h2)
        lay.addRow("方法", self.method)
        lay.addRow(self.run_btn)
        lay.addRow(self.log)

        self.btn_in.clicked.connect(lambda: self._pick(self.in_edit))
        self.btn_out.clicked.connect(lambda: self._pick(self.out_edit))
        self.run_btn.clicked.connect(self.run)

        self.log_sig.connect(self.log.appendPlainText)  # ★ UI更新はメインスレッド
        add_gui_log_sink(self._sink)                    # ★ pipeline に登録
    
    def _sink(self, msg: str):
        self.log_sig.emit(msg)  # ★ どのスレッドから来ても安全

    def log_print(self, msg):  # 既存呼び出し用（任意）
        self.log_sig.emit(msg)

    def _pick(self, edit):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "フォルダを選択")
        if d: edit.setText(d)

    def run(self):
        parent = self.in_edit.text().strip()
        out    = self.out_edit.text().strip()
        method = self.method.currentText()
        if not os.path.isdir(parent) or not out:
            self.log_print("入力/出力を確認してください"); return
        self.run_btn.setEnabled(False)
        def job():
            try:
                n = run_zproject(parent, out, method=method, max_depth=5, overwrite=False)
                self.log_print(f"[done] {n} file(s) written.")
            except Exception as e:
                self.log_print(f"[error] {e}")
            finally:
                self.run_btn.setEnabled(True)
        threading.Thread(target=job, daemon=True).start()

class CountTab(QtWidgets.QWidget):
    log_sig = QtCore.pyqtSignal(str)   # ★ 受信シグナル
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QtWidgets.QFormLayout(self)

        self.in_edit  = QtWidgets.QLineEdit()
        self.out_edit = QtWidgets.QLineEdit()
        self.btn_in   = QtWidgets.QPushButton("選択")
        self.btn_out  = QtWidgets.QPushButton("選択")
        self.scale    = QtWidgets.QDoubleSpinBox(); self.scale.setRange(0.3, 2.0); self.scale.setValue(0.7); self.scale.setSingleStep(0.05)
        self.diam     = QtWidgets.QSpinBox(); self.diam.setRange(3, 200); self.diam.setValue(30)
        self.invert   = QtWidgets.QCheckBox("明視野で細胞が暗い（invert）"); self.invert.setChecked(True)
        self.cellprob = QtWidgets.QDoubleSpinBox(); self.cellprob.setRange(-1.0, 1.0); self.cellprob.setSingleStep(0.05); self.cellprob.setValue(0.15)
        self.flowthr  = QtWidgets.QDoubleSpinBox(); self.flowthr.setRange(0.0, 1.0); self.flowthr.setSingleStep(0.05); self.flowthr.setValue(0.4)
        self.minfrac  = QtWidgets.QDoubleSpinBox(); self.minfrac.setRange(0.05, 1.0); self.minfrac.setSingleStep(0.05); self.minfrac.setValue(0.25)
        self.circmax  = QtWidgets.QDoubleSpinBox(); self.circmax.setRange(0.0, 1.0); self.circmax.setSingleStep(0.05); self.circmax.setValue(0.80)
        self.overlay  = QtWidgets.QCheckBox("オーバーレイPNG保存"); self.overlay.setChecked(True)
        self.btn_single = QtWidgets.QPushButton("単発テスト")

        # ... 本命
        self.run_btn  = QtWidgets.QPushButton("Cell Count 実行")
        self.log      = QtWidgets.QPlainTextEdit(); self.log.setReadOnly(True)

        # rows
        h1 = QtWidgets.QHBoxLayout(); h1.addWidget(self.in_edit);  h1.addWidget(self.btn_in)
        h2 = QtWidgets.QHBoxLayout(); h2.addWidget(self.out_edit); h2.addWidget(self.btn_out)
        lay.addRow("入力ルート（投影画像ファイルの親 or 視野フォルダ）", h1)
        lay.addRow("出力ルート", h2)
        lay.addRow("scale", self.scale)
        lay.addRow("diameter_px", self.diam)
        lay.addRow(self.invert)
        lay.addRow("cellprob_threshold", self.cellprob)
        lay.addRow("flow_threshold", self.flowthr)
        lay.addRow("min_diam_frac", self.minfrac)
        lay.addRow("circularity_max", self.circmax)
        lay.addRow(self.overlay)
        lay.addRow(self.btn_single)
        lay.addRow(self.run_btn)
        lay.addRow(self.log)

        self.btn_in.clicked.connect(lambda: self._pick(self.in_edit))
        self.btn_out.clicked.connect(lambda: self._pick(self.out_edit))
        self.btn_single.clicked.connect(self.single_test)
        self.run_btn.clicked.connect(self.run)

        # モデルは最初に初期化（重いので1回）
        self.model = None
        self._init_model()

        # ログ出力
        self.log_sig.connect(self.log.appendPlainText)
        add_gui_log_sink(self._sink)

    def _sink(self, msg: str):
        self.log_sig.emit(msg)

    def log_print(self, msg):
        self.log_sig.emit(msg)
    
    def _pick(self, edit):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "フォルダを選択")
        if d: edit.setText(d)

    def _init_model(self):
        self.log_print("Initializing model (GPU優先)...")
        def job():
            try:
                self.model = init_model(use_gpu=True)
                self.log_print("[OK] Model ready (GPU)")
            except Exception as e:
                self.log_print(f"[WARN] GPU失敗: {e} -> CPUで初期化します")
                self.model = init_model(use_gpu=False)
                self.log_print("[OK] Model ready (CPU)")
        threading.Thread(target=job, daemon=True).start()

    def run(self):
        if self.model is None:
            self.log_print("モデル初期化中です…少し待ってから実行してください。")
            return
        in_dir  = self.in_edit.text().strip()
        out_dir = self.out_edit.text().strip()
        if not os.path.isdir(in_dir) or not out_dir:
            self.log_print("入力/出力を確認してください"); return

        params = dict(
            scale=self.scale.value(),
            diameter_px=self.diam.value(),
            invert=self.invert.isChecked(),
            cellprob_threshold=self.cellprob.value(),
            flow_threshold=self.flowthr.value(),
            min_diam_frac=self.minfrac.value(),
            circularity_max=self.circmax.value(),
            save_overlay=self.overlay.isChecked(),
        )
        self.run_btn.setEnabled(False)
        self.log_print(f"Running with params: {params}")

        def job():
            try:
                wide_csv, long_csv = run_cellcount(self.model, in_dir, out_dir, **params)
                self.log_print(f"[done] CSV saved:\n  WIDE: {os.path.abspath(wide_csv)}\n  LONG: {os.path.abspath(long_csv)}")
                if params["save_overlay"]:
                    self.log_print(f"Overlays: {os.path.abspath(os.path.join(out_dir,'viz'))}")
            except Exception as e:
                self.log_print(f"[error] {e}")
            finally:
                self.run_btn.setEnabled(True)

        threading.Thread(target=job, daemon=True).start()
    
    def single_test(self):
        if self.model is None:
            self.log_print("モデル初期化中です…少し待ってください。"); return
        self.btn_single.setEnabled(False)
    
        def job():
            try:
                from pipeline import single_test_handler
                single_test_handler(
                    self.model,
                    scale=self.scale.value(),
                    diameter_px=self.diam.value(),
                    invert=self.invert.isChecked(),
                    cellprob_threshold=self.cellprob.value(),
                    flow_threshold=self.flowthr.value(),
                    min_diam_frac=self.minfrac.value(),
                    circularity_max=self.circmax.value(),
                    out_root=self.out_edit.text().strip() or ".",
                    parent=self,
                )
            finally:
                QtCore.QMetaObject.invokeMethod(
                    self.btn_single, "setEnabled",
                    QtCore.Qt.QueuedConnection,
                    QtCore.Q_ARG(bool, True)
                )
        threading.Thread(target=job, daemon=True).start()


class MainWindow(QtWidgets.QTabWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("CellCountApp")
        self.resize(900, 700)
        self.addTab(ZProjTab(), "Z-Projection")
        self.addTab(CountTab(), "Cell Count")

def main():
    app = QtWidgets.QApplication(sys.argv)
    w = MainWindow(); w.show()
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()