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
from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from ee_workbench.gui.forms import select  # noqa: E402
from ee_workbench.gui.main_window import MainWindow, parse_target  # noqa: E402
from ee_workbench.gui.widgets import dark_palette  # noqa: E402

OUT = ROOT / "docs" / "images"
OUT.mkdir(parents=True, exist_ok=True)

app = QApplication([])
# the tests ask before turning an output on; answer yes for the screenshots
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
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
    c = w.panels()[0].capture_tab
    select(c.trigger, "LEVEL")
    c._trigger_changed()
    c.points.setValue(1024)
    c.count.setValue(3)
    c.cursors_chk.setChecked(True)
    c.start(False)


def shots():
    for i, name in enumerate(("supply-66321d", "supply-6632a", "dmm-3456a")):
        w.tabs.setCurrentIndex(i)
        app.processEvents()
        w.grab().save(str(OUT / f"{name}.png"))
    a = w.panels()[0]
    w.tabs.setCurrentIndex(0)
    a.tabs.setCurrentWidget(a.capture_tab)
    w.grab().save(str(OUT / "capture.png"))
    a.tabs.setCurrentWidget(a.system_scroll)
    w.grab().save(str(OUT / "system.png"))
    t = a.tests_tab
    t.sel_group.button(1).click()
    t.emu_cap.setValue(2.0)
    t.start()
    a.tabs.setCurrentWidget(t)


def tests():
    a = w.panels()[0]
    w.grab().save(str(OUT / "tests.png"))
    a.tests_tab.stop("Stopped")
    a.tabs.setCurrentIndex(a.tabs.count() - 1)
    a.trace_chk.setChecked(True)
    a.worker.request_raw("MEAS:CURR:ACDC?", True)
    a.worker.request_raw("SYST:ERR?", True)


def console():
    w.grab().save(str(OUT / "console.png"))
    w.close()
    ppk_start()


def ppk_start():
    global pw
    pw = MainWindow([parse_target("SIM::PPK2")])
    pw.resize(1440, 900)
    pw.show()

    def power():
        p = pw.panels()[0]
        p.name_edit.setText("sensor node")
        p.span_combo.setCurrentText("30 s")
        p.set_dut_power(True)

    def trend():
        p = pw.panels()[0]
        p.log_chk.setChecked(True)
        app.processEvents()
        pw.grab().save(str(OUT / "ppk2-trend.png"))
        p.tabs.setCurrentIndex(1)
        p.logic_chk.setChecked(True)

    def live():
        pw.grab().save(str(OUT / "ppk2-live.png"))
        pw.close()
        logic_start()

    at(1500, power)
    at(6500, trend)
    at(7200, live)


def logic_start():
    global lw
    lw = MainWindow([parse_target("SIM::SALEAE")])
    lw.resize(1440, 900)
    lw.show()

    def capture():
        p = lw.panels()[0]
        p.name_edit.setText("UART + PWM demo")
        for k in range(4):
            p.dig_chks[k].setChecked(True)
        p.an_chks[0].setChecked(True)
        p.an_chks[1].setChecked(True)
        p.duration.setValue(0.01)
        p.cfg.decoders = [{"name": "Async Serial", "label": "uart",
                           "settings": {"Input Channel": 0, "Bit Rate (Bits/s)": 115200}}]
        p._fill_decoders()
        p.capture_now()

    def wave():
        p = lw.panels()[0]
        p.zoom(0.25)
        p.region_chk.setChecked(True)

    def shots():
        p = lw.panels()[0]
        lw.grab().save(str(OUT / "logic-wave.png"))
        p.tabs.setCurrentIndex(1)
        app.processEvents()
        lw.grab().save(str(OUT / "logic-decoded.png"))
        lw.close()
        app.quit()

    at(1500, capture)
    at(3000, wave)
    at(3600, shots)


at(1500, setup)
at(6000, mid)
at(6500, capture)
at(11000, shots)
at(17000, tests)
at(18000, console)
app.exec()
print("wrote", sorted(p.name for p in OUT.glob("*.png")))
