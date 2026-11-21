# The touch-layout screenshots (1280 x 800). Run through
#   QT_QPA_PLATFORM=offscreen .venv/bin/python scripts/make_screenshots.py --touch
# which sets up `app`, `OUT` and touch mode, then runs this.
from PyQt6.QtCore import QTimer  # noqa: F811
from ee_workbench.gui.connect_dialog import ConnectDialog
from ee_workbench.gui.touch import NumPad

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
                       tw.close(), app.quit()))  # noqa: F821


t_at(1500, t_setup)
t_at(6000, t_shots)
t_at(10000, t_capture)
app.exec()  # noqa: F821
