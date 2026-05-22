"""Unit tests for `mimir.patches.extract_touched_paths`, the
diff-header path-extraction used at ingest and by the backfill CLI.
"""

from mimir.patches import extract_touched_paths


def test_extract_returns_empty_for_non_patch_body():
    """A plain prose body returns the empty set, extraction is
    strictly diff-driven."""
    body = (
        "Hi all,\n\n"
        "I was thinking we should touch fs/foo/bar.c and add a\n"
        "missing wakeup. Thoughts?\n\n"
        "Thanks,\nA\n"
    )
    assert extract_touched_paths(body) == set()


def test_extract_returns_empty_for_none_or_empty():
    assert extract_touched_paths(None) == set()
    assert extract_touched_paths("") == set()


def test_extract_single_file_diff():
    body = (
        "Subject says it all.\n\n"
        "Signed-off-by: Author <a@example>\n\n"
        "diff --git a/fs/btrfs/extent_io.c b/fs/btrfs/extent_io.c\n"
        "index 0000000..1111111 100644\n"
        "--- a/fs/btrfs/extent_io.c\n"
        "+++ b/fs/btrfs/extent_io.c\n"
        "@@ -1,3 +1,4 @@\n"
        " line\n"
        "+new\n"
    )
    assert extract_touched_paths(body) == {"fs/btrfs/extent_io.c"}


def test_extract_multiple_files_in_one_patch():
    """Multi-file diff: every `diff --git` header lands in the set."""
    body = (
        "diff --git a/fs/foo/a.c b/fs/foo/a.c\n"
        "@@ -1 +1 @@\n"
        "-x\n+y\n"
        "diff --git a/include/uapi/foo.h b/include/uapi/foo.h\n"
        "@@ -1 +1 @@\n"
        "-x\n+y\n"
    )
    assert extract_touched_paths(body) == {
        "fs/foo/a.c",
        "include/uapi/foo.h",
    }


def test_extract_uses_b_side_on_rename():
    """Renames carry `a/<old>` and `b/<new>`. The post-rename
    destination is the path that reviewers and MAINTAINERS globs
    care about; that's the `b/` side."""
    body = (
        "diff --git a/old/path.c b/new/path.c\n"
        "similarity index 98%\n"
        "rename from old/path.c\n"
        "rename to new/path.c\n"
        "@@ -1 +1 @@\n"
        "-x\n+y\n"
    )
    paths = extract_touched_paths(body)
    assert paths == {"new/path.c"}
    assert "old/path.c" not in paths


def test_extract_ignores_quoted_diff_lines_in_replies():
    """Reviewer replies often quote a hunk: lines start with `> `.
    The line-start regex anchor means quoted `diff --git` lines
    aren't matched, so a reply that only quotes the original patch
    extracts to the empty set. Replies don't represent files-the-
    patch-touches; the original patch already accounted for them."""
    body = (
        "On Mon, A wrote:\n"
        "> diff --git a/fs/foo/x.c b/fs/foo/x.c\n"
        "> @@ -1 +1 @@\n"
        "> -x\n+y\n"
        "\n"
        "LGTM.\n"
    )
    assert extract_touched_paths(body) == set()


def test_extract_handles_binary_diff_header():
    """Binary diffs still emit a `diff --git` header, capture them
    even though the hunk body is missing. Files matter for the
    subsystem mapping regardless of binary-ness."""
    body = (
        "diff --git a/firmware/blob.bin b/firmware/blob.bin\n"
        "Binary files a/firmware/blob.bin and b/firmware/blob.bin differ\n"
    )
    assert extract_touched_paths(body) == {"firmware/blob.bin"}


def test_extract_deduplicates_repeat_headers():
    """A series cover letter sometimes inlines stat output that
    looks like another `diff --git` line, and patch-with-followup
    bodies may include the same header twice. The set semantics
    naturally collapse duplicates."""
    body = (
        "diff --git a/fs/foo/a.c b/fs/foo/a.c\n"
        "@@ -1 +1 @@\n"
        "-x\n+y\n"
        "diff --git a/fs/foo/a.c b/fs/foo/a.c\n"  # appears again
        "@@ -2 +2 @@\n"
        "-x\n+y\n"
    )
    assert extract_touched_paths(body) == {"fs/foo/a.c"}
