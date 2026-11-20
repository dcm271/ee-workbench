from ee_workbench.config import Entry, Session, load_config, startup_entries


def test_first_run_writes_commented_template(tmp_path):
    path = tmp_path / "config.toml"
    cfg = load_config(path)
    assert path.exists() and "[startup]" in path.read_text()
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
''')
    cfg = load_config(path)
    assert cfg.open == "list" and cfg.wait_for_gpib == 5
    plan = startup_entries(cfg, Session(tmp_path / "s.json"))
    assert [(e.resource, e.model) for e in plan] == [("GPIB0::5::INSTR", "663xx"),
                                                    ("GPIB0::22::INSTR", "3456A")]
    assert plan[1].settings == {"name": "E3610A output", "function": "DCV"}


def test_bad_config_falls_back(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[startup\nopen = ")
    cfg = load_config(path)
    assert cfg.open == "last" and cfg.warning
    path.write_text('[startup]\nopen = "everything"\n')
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
    (tmp_path / "last-session.json").write_text("{not json")
    assert Session(tmp_path / "last-session.json").open == []
