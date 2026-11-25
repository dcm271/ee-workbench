from otterbench.config import (Entry, Session, config_dir, load_config, migrate_old_dir,
                               startup_entries)


def test_first_run_writes_commented_template(tmp_path):
    path = tmp_path / "config.toml"
    cfg = load_config(path)
    assert path.exists() and "[startup]" in path.read_text(encoding="utf-8")
    assert cfg.open == "last" and cfg.instruments == [] and not cfg.warning
    # the template itself parses to the same defaults
    assert load_config(path).open == "last"


def test_list_entries_and_aliases(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('''
[startup]
open = "list"
wait_for_gpib = 5

[[instrument]]
resource = "GPIB0::5::INSTR"
model = "66321D"

[[instrument]]
resource = "GPIB0::22::INSTR"
model = "3456A"
name = "E3610A output"
function = "DCV"
''', encoding="utf-8")
    cfg = load_config(path)
    assert cfg.open == "list" and cfg.wait_for_gpib == 5
    plan = startup_entries(cfg, Session(tmp_path / "s.json"))
    assert [(e.resource, e.model) for e in plan] == [("GPIB0::5::INSTR", "663xx"),
                                                    ("GPIB0::22::INSTR", "3456A")]
    assert plan[1].settings == {"name": "E3610A output", "function": "DCV"}


def test_bad_config_falls_back(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[startup\nopen = ", encoding="utf-8")
    cfg = load_config(path)
    assert cfg.open == "last" and cfg.warning
    path.write_text('[startup]\nopen = "everything"\n', encoding="utf-8")
    cfg = load_config(path)
    assert cfg.open == "last" and "everything" in cfg.warning


def test_session_round_trip_and_last(tmp_path):
    s = Session(tmp_path / "last-session.json")
    s.save([Entry("SIM::66321D", "663xx"),
            Entry("GPIB0::22::INSTR", "3456A", {"name": "rail", "function": "2W Ω"})])
    s2 = Session(tmp_path / "last-session.json")
    plan = startup_entries(load_config(tmp_path / "c.toml"), s2, mode="last")
    assert [(e.resource, e.model) for e in plan] == [("SIM::66321D", "663xx"),
                                                    ("GPIB0::22::INSTR", "3456A")]
    assert plan[1].settings["function"] == "2W Ω"
    # closing everything empties "open" but the instrument stays "known"
    s2.save([])
    s3 = Session(tmp_path / "last-session.json")
    assert s3.open == [] and s3.known["GPIB0::22::INSTR"].model == "3456A"


def test_scan_uses_remembered_models(tmp_path):
    s = Session(tmp_path / "last-session.json")
    s.save([Entry("GPIB0::22::INSTR", "3456A", {"name": "rail"})])
    s.save([])
    cfg = load_config(tmp_path / "c.toml")
    plan = startup_entries(cfg, s, ["GPIB0::5::INSTR", "GPIB0::22::INSTR"], mode="scan")
    assert [(e.resource, e.model) for e in plan] == [("GPIB0::5::INSTR", "auto"),
                                                    ("GPIB0::22::INSTR", "3456A")]
    assert plan[1].settings == {"name": "rail"}


def test_corrupt_session_is_ignored(tmp_path):
    (tmp_path / "last-session.json").write_text("{not json", encoding="utf-8")
    assert Session(tmp_path / "last-session.json").open == []


def _old_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    old = tmp_path / "ee-workbench"
    (old / "setups").mkdir(parents=True)
    (old / "setups" / "a.json").write_text("{}", encoding="utf-8")
    (old / "config.toml").write_text("[startup]\nopen = 'none'\n", encoding="utf-8")
    (old / "crash.log").write_text("old", encoding="utf-8")
    return old


def test_settings_from_the_old_name_are_moved(tmp_path, monkeypatch):
    old = _old_dir(tmp_path, monkeypatch)
    assert migrate_old_dir() == ""
    assert not old.exists()
    assert (config_dir() / "setups" / "a.json").exists()
    assert load_config().open == "none"
    assert migrate_old_dir() == ""


def test_migration_keeps_newer_files(tmp_path, monkeypatch):
    old = _old_dir(tmp_path, monkeypatch)
    config_dir().mkdir()                 # e.g. run.sh logged a crash first
    (config_dir() / "crash.log").write_text("new", encoding="utf-8")
    assert migrate_old_dir() == ""
    assert (config_dir() / "crash.log").read_text(encoding="utf-8") == "new"
    assert (old / "crash.log").read_text(encoding="utf-8") == "old"
    assert (config_dir() / "config.toml").exists()


def test_migration_happens_once(tmp_path, monkeypatch):
    old = _old_dir(tmp_path, monkeypatch)
    config_dir().mkdir()
    (config_dir() / "config.toml").write_text("", encoding="utf-8")
    assert migrate_old_dir() == ""
    assert (old / "config.toml").exists() and not (config_dir() / "setups").exists()


def test_migration_without_old_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert migrate_old_dir() == "" and not config_dir().exists()
