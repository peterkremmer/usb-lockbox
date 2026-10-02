from usblockbox.junk import junk_label
from usblockbox.backends.windows import describe_counts


def test_known_folders_get_plain_names():
    assert "disk-check" in junk_label("FOUND.000") and "disk-check" in junk_label("found.017")
    assert junk_label(".Spotlight-V100") == "Mac Spotlight search index"
    assert junk_label("$RECYCLE.BIN") == "Windows Recycle Bin"
    assert junk_label(".Trash-1000") == "Linux Trash"
    assert junk_label("._photo.jpg") == "Mac metadata file"
    assert junk_label("My Documents") is None and junk_label("FOUND.0") is None


def test_describe_counts_names_the_big_folders():
    s = describe_counts(13, {"FOUND.000": 4000, "FOUND.001": 2375, "Docs": 3}, {"FOUND.000"}, mac_meta=2)
    assert s.startswith("13 at the top level")
    assert "4000 in FOUND.000 (Windows disk-check (chkdsk) recovery folder, hidden)" in s
    assert "2 Mac metadata files" in s
