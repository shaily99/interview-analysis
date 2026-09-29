import pytest

from subtitle_search.codebook import CodebookError, TextCodebook
from subtitle_search.video_codes import VideoCodebook


@pytest.fixture
def book(tmp_path):
    return TextCodebook(tmp_path / "text_codebook.json")


def test_a_text_code_persists_with_colour_and_description(book):
    code = book.add({"name": "trust", "description": "Relies on the output"})

    again = TextCodebook(book.path).get(code["id"])

    assert again["name"] == "trust"
    assert again["description"] == "Relies on the output"
    assert again["color"]


def test_text_codes_have_no_keys(book):
    code = book.add({"name": "trust", "key": "q"})

    assert "key" not in code


def test_text_code_names_are_unique_ignoring_case(book):
    book.add({"name": "Trust"})

    with pytest.raises(CodebookError):
        book.add({"name": "trust"})


def test_find_by_name_ignores_case_and_spacing(book):
    code = book.add({"name": "user trust"})

    assert book.find(" USER   trust ")["id"] == code["id"]
    assert book.find("nope") is None


def test_video_codes_still_take_keys(tmp_path):
    video = VideoCodebook(tmp_path / "video_codebook.json")

    assert video.add({"name": "scroll", "key": "q"})["key"] == "q"
