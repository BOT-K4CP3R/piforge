import json

import numpy as np
import pytest

from piforge.core import Finding, NotFoundError, Report, Severity, jsonable


def test_add_and_counts():
    r = Report("erc")
    f = r.add("ERC.LEVEL_MISMATCH", "error", "5 V into GPIO24", subject="net:ECHO", hint="divider", v=5.0)
    r.add("ERC.I2C_PULLUPS", Severity.INFO, "Pi pull-ups only")
    r.add("ERC.FLOATING_INPUT", "warn", "GPIO27 floats")
    assert f.data == {"v": 5.0}
    assert f.source == "erc"
    assert r.counts() == {"error": 1, "warning": 1, "info": 1}
    assert not r.ok
    assert r.worst == Severity.ERROR
    assert [x.code for x in r.errors] == ["ERC.LEVEL_MISMATCH"]


def test_ok_and_truthiness_of_empty_report():
    r = Report("empty")
    assert r.ok
    assert r.worst is None
    assert bool(r) is True  # a Report must never be falsy just because it is empty


def test_has_and_by_code_prefix():
    r = Report()
    r.add("PRINT.OVERHANG", "warning", "x")
    r.add("PRINT.THIN_WALL", "error", "y")
    assert r.has("PRINT")
    assert r.has("PRINT.OVERHANG", "warning")
    assert not r.has("PRINT.OVERHANG", "error")
    assert len(r.by_code("PRINT")) == 2
    assert r.by_code("PRIN") == []  # prefix must end at a dot


def test_merge_keeps_sources_and_extend_chains():
    a = Report("erc")
    a.add("ERC.X", "info", "a")
    b = Report("print")
    b.add("PRINT.Y", "error", "b")
    m = Report.merge(a, b, None, title="all")
    assert [f.source for f in m] == ["erc", "print"]
    assert not m.ok
    c = Report("c").extend(m).extend([Finding("Z.Z", "info", "z")])
    assert len(c.findings) == 3
    with pytest.raises(TypeError):
        Report().extend(["not a finding"])


def test_json_roundtrip_with_numpy_data():
    r = Report("t")
    r.add("A.B", "warning", "m", area=np.float64(3.5), mask=np.array([1, 2]), size=(1, 2, 3))
    d = json.loads(r.to_json())
    assert d["findings"][0]["data"] == {"area": 3.5, "mask": [1, 2], "size": [1, 2, 3]}
    back = Report.from_dict(d)
    assert back.findings[0].severity == Severity.WARNING
    assert back.findings[0].data["area"] == 3.5


def test_markdown_orders_by_severity():
    r = Report("t")
    r.add("A.INFO", "info", "i")
    r.add("A.ERR", "error", "e", hint="do this")
    md = r.to_markdown()
    assert md.index("A.ERR") < md.index("A.INFO")
    assert "FAIL" in md and "fix: do this" in md
    assert "A.INFO" not in r.to_markdown(include_info=False)


def test_severity_parse_and_bad_code():
    assert Severity.parse("WARN") == Severity.WARNING
    assert Severity.parse(2) == Severity.ERROR
    with pytest.raises(ValueError):
        Severity.parse("fatal")
    with pytest.raises(ValueError):
        Finding("", "info", "no code")


def test_jsonable_handles_nan_and_paths(tmp_path):
    assert jsonable(float("nan")) is None
    assert jsonable(tmp_path) == str(tmp_path)


def test_not_found_error_suggests():
    e = NotFoundError("printer", "prusa_mk3", ["prusa_mk4", "prusa_mini", "bambu_a1"])
    assert "prusa_mk4" in str(e)
    assert e.suggestions[0] == "prusa_mk4"
    e2 = NotFoundError("board", "zzz", ["rpi4b", "rpi5"])
    assert "Available: rpi4b, rpi5" in str(e2)
    assert isinstance(e2, LookupError)
