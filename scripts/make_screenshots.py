"""Regenerate the user-guide screenshots from the simulators.

    QT_QPA_PLATFORM=offscreen .venv/bin/python scripts/make_screenshots.py
"""
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PyQt6.QtCore import QTimer  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from ee_workbench.gui.main_window import MainWindow, parse_target  # noqa: E402
from ee_workbench.gui.widgets import dark_palette  # noqa: E402

OUT = ROOT / "docs" / "images"
OUT.mkdir(parents=True, exist_ok=True)

app = QApplication([])
dark_palette(app)
empty = MainWindow()
empty.resize(1280, 720)
empty.show()
app.processEvents()
empty.grab().save(str(OUT / "start.png"))
empty.close()

w = MainWindow([parse_target(t) for t in ("SIM::66321D", "SIM::6632A", "SIM::3456A")])
w.resize(1440, 900)
w.show()


def at(ms, fn):
    QTimer.singleShot(ms, fn)


def setup():
    a, b, c = w.panels()
    a.sp_volt.setValue(3.8); a.sp_volt.lineEdit().returnPressed.emit()
    a.sp_curr.setValue(1.5); a.sp_curr.lineEdit().returnPressed.emit()
    a.dvm_chk.setChecked(True); a.pulse_chk.setChecked(True)
    a.set_output(True)
    a._apply_sweep(1024, 15.6)
    b.sp_volt.setValue(12); b.sp_volt.lineEdit().returnPressed.emit()
    b.sp_curr.setValue(2); b.sp_curr.lineEdit().returnPressed.emit()
    b.set_output(True)
    c.name_edit.setText("E3610A output")


def mid():
    a = w.panels()[0]
    a.sp_volt.setValue(4.2)  # leave un-applied to show the amber "pending" state


def capture():
    a = w.panels()[0]
    a.wf_points.setValue(4096)
    a.capture()


def shots():
    for i, name in enumerate(("supply-66321d", "supply-6632a", "dmm-3456a")):
        w.tabs.setCurrentIndex(i)
        app.processEvents()
        w.grab().save(str(OUT / f"{name}.png"))
    a = w.panels()[0]
    w.tabs.setCurrentIndex(0)
    a.tabs.setCurrentIndex(1)
    app.processEvents()
    w.grab().save(str(OUT / "waveform.png"))
    a.tabs.setCurrentIndex(a.tabs.count() - 1)
    a.trace_chk.setChecked(True)
    a.worker.request_raw("MEAS:CURR:ACDC?", True)
    a.worker.request_raw("SYST:ERR?", True)


def console():
    w.grab().save(str(OUT / "console.png"))
    w.close()
    app.quit()


at(1500, setup)
at(6000, mid)
at(6500, capture)
at(11000, shots)
at(12000, console)
app.exec()
print("wrote", sorted(p.name for p in OUT.glob("*.png")))
