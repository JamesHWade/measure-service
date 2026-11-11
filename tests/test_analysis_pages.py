import json

import pytest

from measure_service.analysis_pages import MAX_PAGE, MAX_RESPONSE_BYTES, page_analysis_data


def _compact_bytes(value):
    return len(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    )


def test_peak_pages_preserve_rows_and_report_actual_continuation():
    peaks = [{"peak_id": index, "location": index / 10, "label": "β"} for index in range(5)]

    first = page_analysis_data("analysis-1", "peaks", {"peaks": peaks}, 0, 2)
    second = page_analysis_data("analysis-1", "peaks", {"peaks": peaks}, 2, 2)
    final = page_analysis_data("analysis-1", "peaks", {"peaks": peaks}, 4, 2)

    assert first == {
        "analysis_id": "analysis-1",
        "kind": "peaks",
        "offset": 0,
        "count": 5,
        "next_offset": 2,
        "data": peaks[:2],
    }
    assert second["data"] == peaks[2:4]
    assert second["next_offset"] == 4
    assert final["data"] == peaks[4:]
    assert final["count"] == 5
    assert final["next_offset"] is None


@pytest.mark.parametrize(
    ("kind", "data"),
    [
        ("processed_trace", {"axis": [0.0, 1.0, 2.0], "signal": [2.0, 3.0, 4.0]}),
        (
            "distribution",
            {
                "log10_mw": [4.0, 5.0, 6.0],
                "differential": [0.2, 0.5, 0.3],
                "cumulative": [0.0, 0.5, 1.0],
            },
        ),
    ],
)
def test_existing_array_result_shapes_page_in_lockstep(kind, data):
    result = {kind: data}
    first = page_analysis_data("analysis-2", kind, result, 1, 1)
    end = page_analysis_data("analysis-2", kind, result, 3, 1)

    assert first["count"] == 3
    assert first["data"] == {key: [values[1]] for key, values in data.items()}
    assert first["next_offset"] == 2
    assert end["count"] == 3
    assert end["data"] == {key: [] for key in data}
    assert end["next_offset"] is None


def test_all_peak_rows_are_returned_once_in_contiguous_pages():
    peaks = [{"peak_id": index, "area": index / 2} for index in range(11)]
    collected = []
    offset = 0

    while True:
        page = page_analysis_data("analysis-3", "peaks", {"peaks": peaks}, offset, 3)
        collected.extend(page["data"])
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]

    assert collected == peaks


def test_all_array_rows_are_returned_once_in_contiguous_pages():
    data = {
        "axis": [index / 10 for index in range(11)],
        "signal": [float(index) for index in range(11)],
    }
    collected = {key: [] for key in data}
    offset = 0

    while True:
        page = page_analysis_data(
            "analysis-array", "processed_trace", {"processed_trace": data}, offset, 3
        )
        for key, values in page["data"].items():
            collected[key].extend(values)
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]

    assert collected == data


def test_unicode_peak_pages_obey_compact_canonical_utf8_byte_budget():
    peaks = [{"peak_id": index, "description": "é" * 11_000} for index in range(4)]
    result = {"peaks": peaks}

    first = page_analysis_data("analysis-4", "peaks", result, 0, 4)
    second = page_analysis_data("analysis-4", "peaks", result, first["next_offset"], 4)

    assert 0 < len(first["data"]) < 4
    assert first["count"] == len(peaks)
    assert first["next_offset"] == len(first["data"])
    assert _compact_bytes(first) <= MAX_RESPONSE_BYTES
    assert second["data"] == peaks[first["next_offset"] :]
    assert _compact_bytes(second) <= MAX_RESPONSE_BYTES


def test_empty_and_past_end_pages_keep_total_count_and_have_no_continuation():
    peaks = [{"peak_id": 1}]

    empty = page_analysis_data("analysis-5", "peaks", {"peaks": []}, 0, 10)
    past_end = page_analysis_data("analysis-5", "peaks", {"peaks": peaks}, 20, 10)

    assert empty["count"] == 0 and empty["data"] == []
    assert empty["next_offset"] is None
    assert past_end["count"] == 1 and past_end["data"] == []
    assert past_end["next_offset"] is None


def test_rejects_a_single_peak_row_that_cannot_fit_the_response_limit():
    result = {"peaks": [{"description": "x" * MAX_RESPONSE_BYTES}]}

    with pytest.raises(ValueError, match="single analysis row.*response limit"):
        page_analysis_data("analysis-6", "peaks", result, 0, 1)


@pytest.mark.parametrize(
    ("kind", "result"),
    [
        ("peaks", {}),
        ("peaks", {"peaks": ([],)}),
        ("peaks", {"peaks": [None]}),
        ("peaks", {"peaks": [{1: "numeric key"}]}),
        ("processed_trace", {"processed_trace": {"axis": [0.0], "signal": [0.0, 1.0]}}),
        ("processed_trace", {"processed_trace": {"axis": ["0"], "signal": [0.0]}}),
        ("processed_trace", {"processed_trace": {"axis": [float("nan")], "signal": [0.0]}}),
        ("processed_trace", {"processed_trace": {"axis": [0.0], "other": [1.0]}}),
        (
            "distribution",
            {
                "distribution": {
                    "log10_mw": [4.0],
                    "differential": [float("inf")],
                    "cumulative": [1.0],
                }
            },
        ),
    ],
)
def test_malformed_stored_data_fails_closed(kind, result):
    with pytest.raises(ValueError):
        page_analysis_data("analysis-7", kind, result, 0, 1)


@pytest.mark.parametrize(
    ("offset", "limit"),
    [
        (True, 1),
        (0.0, 1),
        (-1, 1),
        (0, True),
        (0, 0),
        (0, MAX_PAGE + 1),
        (0, 1.5),
    ],
)
def test_page_bounds_require_plain_integers(offset, limit):
    with pytest.raises(ValueError):
        page_analysis_data("analysis-8", "peaks", {"peaks": []}, offset, limit)


def test_unsupported_kind_fails_as_a_bounded_value_error():
    with pytest.raises(ValueError, match="kind must be"):
        page_analysis_data("analysis-9", [], {"peaks": []}, 0, 1)
