

def test_a_run_ended_by_a_restart_is_told_apart(tmp_path, monkeypatch):
    import time as _t
    from otterbench import safety as sf
    monkeypatch.setattr(sf, "boot_time", lambda: _t.time() - 60)       # booted a minute ago
    old = {"started": _t.strftime("%Y-%m-%dT%H:%M:%S", _t.localtime(_t.time() - 3600))}
    new = {"started": _t.strftime("%Y-%m-%dT%H:%M:%S", _t.localtime(_t.time() - 10))}
    assert sf.ended_by_restart(old) is True
    assert sf.ended_by_restart(new) is False                         # since the boot: a crash
    assert sf.ended_by_restart({"started": ""}) is False
    monkeypatch.setattr(sf, "boot_time", lambda: None)
    assert sf.ended_by_restart(old) is False


def test_boot_time_is_in_the_past():
    import time as _t
    from otterbench import safety as sf
    b = sf.boot_time()
    assert b is None or 0 < b < _t.time()
