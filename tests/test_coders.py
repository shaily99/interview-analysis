import pytest

from subtitle_search.coders import CoderDirectory, CoderError, suggest_initials


@pytest.fixture
def coders(tmp_path):
    return CoderDirectory(tmp_path)


def test_a_new_coder_is_written_to_their_own_folder(tmp_path, coders):
    coder = coders.create("Shaily Bhatt", "SB")

    assert (tmp_path / "coders" / coder["id"] / "coder.json").is_file()
    assert CoderDirectory(tmp_path).get(coder["id"])["name"] == "Shaily Bhatt"


def test_the_same_name_continues_as_that_coder(coders):
    first = coders.create("Shaily Bhatt", "SB")

    again = coders.create("  shaily   BHATT ", "ZZ")

    assert again["id"] == first["id"]
    assert again["initials"] == "SB"
    assert len(coders.list()) == 1


def test_initials_must_be_unique_ignoring_case(coders):
    coders.create("Shaily Bhatt", "SB")

    with pytest.raises(CoderError):
        coders.create("Sam Brown", "sb")


def test_a_coder_needs_a_name_and_initials(coders):
    with pytest.raises(CoderError):
        coders.create("   ", "SB")
    with pytest.raises(CoderError):
        coders.create("Sam Brown", "  ")


@pytest.mark.parametrize(
    "name,taken,expected",
    [
        ("Shaily Bhatt", [], "SB"),
        ("Shaily Bhatt", ["SB"], "SBh"),
        ("Shaily Bhatt", ["SB", "SBH"], "SBha"),
        ("Rachel", [], "R"),
        ("rachel k.", [], "RK"),
    ],
)
def test_suggested_initials_grow_until_unique(name, taken, expected):
    assert suggest_initials(name, taken) == expected


def test_a_coder_can_change_name_and_initials(coders):
    coder = coders.create("Shaily Bhatt", "SB")

    coders.update(coder["id"], {"name": "Shaily J. Bhatt", "initials": "SJB"})

    assert CoderDirectory(coders.root).get(coder["id"])["initials"] == "SJB"


def test_changing_initials_to_someone_elses_is_refused(coders):
    mine = coders.create("Shaily Bhatt", "SB")
    coders.create("Rachel K.", "RK")

    with pytest.raises(CoderError):
        coders.update(mine["id"], {"initials": "rk"})


def test_updating_an_unknown_coder_raises_key_error(coders):
    with pytest.raises(KeyError):
        coders.update("nope", {"name": "x"})


def test_folders_without_a_coder_file_are_skipped(tmp_path, coders):
    (tmp_path / "coders" / "stray").mkdir(parents=True)
    coders.create("Shaily Bhatt", "SB")

    assert [c["name"] for c in CoderDirectory(tmp_path).list()] == ["Shaily Bhatt"]


def test_a_new_coder_given_only_a_name_gets_unique_initials(coders):
    coders.create("Shaily Bhatt", "SB")

    assert coders.create("Sam Brown", None)["initials"] == "SBr"
