# The touch-layout screenshots (1280 x 800). Run through
#   QT_QPA_PLATFORM=offscreen .venv/bin/python scripts/make_screenshots.py --touch
# which sets up `app`, `OUT` and touch mode, then runs this.
from PyQt6.QtCore import QTimer  # noqa: F811
from otterbench.gui.connect_dialog import ConnectDialog
from otterbench.gui.touch import NumPad

tw = MainWindow([parse_target(t) for t in ("SIM::66321D", "SIM::3456A")])  # noqa: F821
tw.resize(1280, 800)
tw.show()


def t_at(ms, fn):
    QTimer.singleShot(ms, fn)


def t_setup():
    a, d = tw.panels()
    a.sp_volt.setValue(3.8); a.sp_volt.lineEdit().returnPressed.emit()
    a.sp_curr.setValue(1.5); a.sp_curr.lineEdit().returnPressed.emit()
    a.dvm_chk.setChecked(True)
    a.set_output(True)
    d.name_edit.setText("E3610A output")


def t_shots():
    a = tw.panels()[0]
    tw.tabs.setCurrentIndex(0)
    app.processEvents()  # noqa: F821
    tw.grab().save(str(OUT / "touch-supply.png"))  # noqa: F821
    tw.tabs.setCurrentIndex(1)
    app.processEvents()  # noqa: F821
    tw.grab().save(str(OUT / "touch-dmm.png"))  # noqa: F821
    tw.tabs.setCurrentIndex(0)
    a.tabs.setCurrentWidget(a.capture_tab)
    a.capture_tab.start(False)


def t_capture():
    tw.grab().save(str(OUT / "touch-capture.png"))  # noqa: F821
    pad = NumPad(tw, "Voltage", 3.8, 0.0, 20.0, 4, " V")
    pad.show()
    app.processEvents()  # noqa: F821
    pad.grab().save(str(OUT / "touch-keypad.png"))  # noqa: F821
    pad.close()
    dlg = ConnectDialog(tw, set())
    dlg.resize(1100, 700)
    dlg.show()
    t_at(800, lambda: (dlg.grab().save(str(OUT / "touch-connect.png")), dlg.close(),  # noqa: F821
                       tw.close(), t_new()))  # noqa: F821


def t_new():
    """The generic SCPI and scope tabs, production test and safety on the Pi screen."""
    global nw
    nw = MainWindow([parse_target(t) for t in ("SIM::DP832", "SIM::SCOPE")])  # noqa: F821
    nw.resize(1280, 800)
    nw.show()

    def setup():
        sup, scope = nw.panels()
        sup.name_edit.setText("rails")
        for n, v in (("vset1", 3.3), ("out1", True), ("vset2", 5.0)):
            sup._set(n, v)
        scope.capture_now()

    def shots():
        for i, name in enumerate(("touch-scpi", "touch-scope")):
            nw.tabs.setCurrentIndex(i)
            app.processEvents()  # noqa: F821
            nw.grab().save(str(OUT / f"{name}.png"))  # noqa: F821
        import tempfile
        nw.prefs["production"] = {"product": "Sensor board rev C", "operator": "DM",
                                  "db": str(Path(tempfile.mkdtemp()) / "r.db")}  # noqa: F821
        pw = nw.show_production()
        pw.showNormal()                # the offscreen screen is 800 x 800: size it as the Pi's
        pw.resize(1280, 800)
        pw._show("FAIL", "FAIL  SB000103")
        pw.detail.setText("I_load (A)=0.899994 (want 0.7 … 0.8)")
        app.processEvents()  # noqa: F821
        pw.grab().save(str(OUT / "touch-production.png"))  # noqa: F821
        pw.close()
        from otterbench.gui.safety_dialog import SafetyDialog
        from otterbench.safety import Safety
        d = SafetyDialog(nw, Safety(off_on_quit=True, auto_reconnect=True))
        d.show()
        app.processEvents()  # noqa: F821
        d.grab().save(str(OUT / "touch-safety.png"))  # noqa: F821
        d.close()
        nw.all_outputs_off()
        t_at(500, lambda: (nw.close(), t_dual()))

    t_at(2500, setup)
    t_at(6000, shots)


def t_dual():
    """A dual-output supply: both outputs side by side."""
    global dw
    dw = MainWindow([parse_target("SIM::66309B")])  # noqa: F821
    dw.resize(1280, 800)
    dw.show()

    def setup():
        p = dw.panels()[0]
        p.cmd(lambda d: (d.set_voltage(3.8), d.set_current(1.5), d.set_voltage2(5),
                         d.set_current2(1), d.set_output(True)))

    def shot():
        dw.grab().save(str(OUT / "touch-66309b.png"))  # noqa: F821
        dw.all_outputs_off()
        t_at(500, lambda: (dw.close(), app.quit()))  # noqa: F821

    t_at(1500, setup)
    t_at(5000, shot)


t_at(1500, t_setup)
t_at(6000, t_shots)
t_at(10000, t_capture)
app.exec()  # noqa: F821
