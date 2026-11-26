import pytest
from openpyxl import load_workbook

from workbook_export import build_workbook


def test_excel_roundtrip_and_literal_cells():
    with build_workbook([["名称", "数量"], ["测试", 42], ["=1+1", True], [None, 2.5]]) as buf:
        wb = load_workbook(buf)
        assert list(wb.active.values) == [("名称", "数量"), ("测试", 42), ("=1+1", True), (None, 2.5)]
        assert wb.active["A3"].data_type == "s"


@pytest.mark.parametrize("rows", [[], [["x"*32768]], [[0]*257], [[1]]*5001, [[float("nan")]], [["bad\x01"]]])
def test_invalid_workbook_inputs(rows):
    with pytest.raises(ValueError):
        build_workbook(rows)
