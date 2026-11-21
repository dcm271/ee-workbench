"""Regenerate the user-guide screenshots from the simulators.

    QT_QPA_PLATFORM=offscreen .venv/bin/python scripts/make_screenshots.py
    QT_QPA_PLATFORM=offscreen .venv/bin/python scripts/make_screenshots.py --touch
"""
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PyQt6.QtCore import QTimer  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from ee_workbench.gui import touch as touch_mode  # noqa: E402
from ee_workbench.gui.forms import select  # noqa: E402
from ee_workbench.gui.main_window import MainWindow, parse_target  # noqa: E402
from ee_workbench.gui.widgets import dark_palette  # noqa: E402

OUT = ROOT / "docs" / "images"
OUT.mkdir(parents=True, exist_ok=True)

TOUCH = "--touch" in sys.argv
app = QApplication([])
# the tests ask before turning an output on; answer yes for the screenshots
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
dark_palette(app)
if TOUCH:
    touch_mode.detect(app, True)
    app.setStyleSheet(app.styleSheet() + touch_mode.TOUCH_STYLE)
    exec(open(Path(__file__).with_name("touch_screenshots.py")).read())
    sys.exit(0)
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
        features_start()

    at(1500, capture)
    at(3000, wave)
    at(3600, shots)


def features_start():
    """PPK2 alignment, the sequencer and a recorded session."""
    import tempfile

    from ee_workbench.gui.logic_panel import LogicPanel
    from ee_workbench.gui.ppk2_panel import Ppk2Panel
    from ee_workbench.gui.session_viewer import SessionViewer
    from ee_workbench.sequencer import Step
    from ee_workbench.session import recorder
    global fw, sess_path
    sess_path = str(Path(tempfile.mkdtemp()) / "demo.eewb")
    fw = MainWindow([parse_target(t) for t in ("SIM::66321D", "SIM::PPK2", "SIM::SIGROK")])
    fw.resize(1440, 900)
    fw.show()

    def lp():
        return next(p for p in fw.panels() if isinstance(p, LogicPanel))

    def pp():
        return next(p for p in fw.panels() if isinstance(p, Ppk2Panel))

    def start():
        recorder.start(sess_path)
        fw._sess_timer.start()
        p = pp()
        p.name_edit.setText("sensor node")
        p.set_dut_power(True)

    def capture():
        q = lp()
        q.name_edit.setText("debug UART")
        q._fill_ppk_choices()
        q.align_ppk.setCurrentIndex(1)
        q.align_ch.setCurrentIndex(q.align_ch.findData(7))
        q.align_bit.setCurrentIndex(7)
        for k in (0, 7):
            q.dig_chks[k].setChecked(True)
        q.duration.setValue(0.3)
        q.d_rate.setCurrentIndex(q.d_rate.findData(2e6))
        q.cfg.decoders = [{"name": "UART", "label": "uart",
                           "settings": {"rx": 0, "baudrate": 115200}}]
        q._fill_decoders()
        q.capture_now()
        recorder.note("UART burst captured")

    def align_shot():
        fw.tabs.setCurrentWidget(lp())
        lp().fit()
        app.processEvents()
        fw.grab().save(str(OUT / "logic-align.png"))
        fw.show_sequencer()
        s = fw.seq_win
        s.resize(1440, 900)
        s.table.setRowCount(0)
        for st in (Step("Set", "PPK2", "voltage", "x"), Step("Set", "66321D", "voltage", "x"),
                   Step("Set", "66321D", "output", "on"), Step("Wait", "", "", "0.3"),
                   Step("Measure", "PPK2", "current", "0.5", "I_node", "", "0.0002"),
                   Step("Measure", "66321D", "current", "", "I_supply"),
                   Step("Note", "", "", "pass {i}: {x} V")):
            s.add_step(st)
        s.loop.setCurrentIndex(2)
        s.x_from.setValue(1.8)
        s.x_to.setValue(3.6)
        s.x_step.setValue(0.3)
        s.off_chk.setChecked(False)
        s.run()

    def seq_shot():
        s = fw.seq_win
        s.y_col.setCurrentText("I_node")
        s.grab().save(str(OUT / "sequencer.png"))
        s.close()
        recorder.stop()
        fw._sess_timer.stop()
        global viewer
        viewer = SessionViewer(sess_path)
        viewer.resize(1440, 900)
        viewer.show()

    def viewer_shot():
        viewer.grab().save(str(OUT / "session-viewer.png"))
        c = viewer.s.captures[0]
        a, _ = c.span
        viewer.go(a + 0.05, a + 0.062)
        at(400, viewer_zoom)

    def viewer_zoom():
        viewer.grab().save(str(OUT / "session-zoom.png"))
        viewer.fit()
        viewer.cur_chk.setChecked(True)
        x0, x1 = viewer.plots[0].viewRange()[0]
        viewer.cursors[0][0].setValue(x0 + (x1 - x0) * 0.25)
        viewer.cursors[0][1].setValue(x0 + (x1 - x0) * 0.6)
        app.processEvents()
        viewer.grab().save(str(OUT / "session-cursors.png"))
        viewer.close()
        fw.close()
        bench_start()

    at(1500, start)
    at(4000, capture)
    at(6500, align_shot)
    at(21000, seq_shot)
    at(22000, viewer_shot)


def bench_start():
    """GPIO, function generator, profiles, events, math, watchdog, setups."""
    from ee_workbench.gui.hub import hub
    from ee_workbench.gui.setups_dialog import SetupsDialog
    from ee_workbench.mathchannels import Channel
    from ee_workbench.watchdog import Rule
    global bw
    bw = MainWindow([parse_target(t) for t in ("SIM::66321D", "SIM::PPK2", "SIM::GPIO",
                                               "SIM::FGEN")])
    bw.resize(1440, 900)
    bw.show()
    P = {}

    def tab(panel, text):
        panel.tabs.setCurrentIndex([panel.tabs.tabText(i) for i in
                                    range(panel.tabs.count())].index(text))

    def setup():
        for p in bw.panels():
            P[p.KIND] = p
        a, pp, g, f = P["supply"], P["ppk2"], P["gpio"], P["fgen"]
        a.sp_volt.setValue(3.3)
        a.sp_volt.lineEdit().returnPressed.emit()
        a.set_output(True)
        pp.name_edit.setText("sensor node")
        pp.set_dut_power(True)
        g.name_edit.setText("jig")
        for gp, name, mode, pull, al in ((20, "relay", 1, "none", True),
                                         (21, "done", 2, "none", False),
                                         (26, "button", 2, "up", False),
                                         (16, "reset", 1, "none", False)):
            r = g._rows[gp]
            r["name"].setText(name)
            r["mode"].setCurrentIndex(mode)
            r["pull"].setCurrentText(pull)
            r["al"].setChecked(al)
        g._apply()
        g.used_only.setChecked(True)
        f.name_edit.setText("clock in")
        f.freq.setValue(32768)
        f.freq.editingFinished.emit()
        f.func.setCurrentIndex(1)
        f.func.activated.emit(1)
        f.ampl.setValue(3.3)
        f.ampl.editingFinished.emit()
        f.offs.setValue(1.65)
        f.offs.editingFinished.emit()

    def run1():
        P["gpio"]._toggle(20, True)
        a = P["supply"]
        tab(a, "Profile")
        a.profile_box.text.setCurrentText("sine 3.0..3.6 period=2 for=20 rate=10")
        a.profile_box.start()

    def shots1():
        for kind, name in (("gpio", "gpio"), ("fgen", "fgen"), ("supply", "supply-profile")):
            bw.tabs.setCurrentWidget(P[kind])
            app.processEvents()
            bw.grab().save(str(OUT / f"{name}.png"))
        pp = P["ppk2"]
        bw.tabs.setCurrentWidget(pp)
        tab(pp, "Events")
        pp.events_tab.thr.setValue(1.0)
        pp.events_tab.find()
        pp.events_tab.table.selectRow(0)
        app.processEvents()
        bw.grab().save(str(OUT / "ppk2-events.png"))
        from ee_workbench.signals import slug
        rail = slug(P["supply"].display_name())
        hub.math.channels = [Channel("P_rail", f"{rail}.power", "W"),
                             Channel("E_rail", f"{rail}.power", "J", True),
                             Channel("I_node_avg", f"avg({slug(P['ppk2'].display_name())}.current, 2) * 1000",
                                     "mA")]
        m = bw.show_math()
        m.resize(1280, 720)
        hub.watchdog.rules = [
            Rule(P["ppk2"].display_name(), "current", ">", 0.05, 10.0),
            Rule(P["supply"].display_name(), "voltage", "<", 2.9, 0.0, "turn off",
                 P["fgen"].display_name()),
            Rule(P["gpio"].display_name(), "gpio26", "<", 0.5, 500.0, "stop sequence")]
        wd = bw.show_watchdog()
        wd.resize(1280, 460)

    def shots2():
        bw.math_win.grab().save(str(OUT / "math.png"))
        bw.wd_win.grab().save(str(OUT / "watchdog.png"))
        bw.math_win.close()
        bw.wd_win.close()
        hub.watchdog.rules = []
        hub.math.channels = []
        from ee_workbench import setups
        from ee_workbench.gui.setups_dialog import capture_bench
        setups.save_setup("nRF52 sleep current", capture_bench(bw), "PPK2 + rail + jig, "
                          "sleep < 10 µA")
        setups.save_setup("Regulator efficiency", capture_bench(bw), "two supplies, "
                          "efficiency math channel")
        d = SetupsDialog(bw)
        d.resize(620, 380)
        d.show()
        app.processEvents()
        d.grab().save(str(OUT / "setups.png"))
        d.close()
        for n in ("nRF52 sleep current", "Regulator efficiency"):
            setups.delete_setup(n)
        bw.all_outputs_off()

    def done():
        bw.close()
        app.quit()

    at(2500, setup)
    at(4500, run1)
    at(8000, shots1)
    at(11500, shots2)
    at(12500, done)


at(1500, setup)
at(6000, mid)
at(6500, capture)
at(11000, shots)
at(17000, tests)
at(18000, console)
app.exec()
print("wrote", sorted(p.name for p in OUT.glob("*.png")))
