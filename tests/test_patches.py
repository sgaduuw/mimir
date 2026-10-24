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


# Unified diffs without git's header (#869): quilt, `diff -u`, `Index:`.


def test_extract_quilt_patch():
    body = (
        "[patch v6.1] net: fix the frobnicator\n\n"
        "Signed-off-by: Author <a@example>\n"
        "---\n"
        " net/core/frob.c |    2 +-\n"
        " 1 file changed, 1 insertion(+), 1 deletion(-)\n\n"
        "--- a/net/core/frob.c\n"
        "+++ b/net/core/frob.c\n"
        "@@ -10,7 +10,7 @@ static int frob(void)\n"
        " \tint ret;\n"
        "-\tret = 0;\n"
        "+\tret = -EINVAL;\n"
    )
    assert extract_touched_paths(body) == {"net/core/frob.c"}


def test_extract_plain_diff_u_drops_timestamps():
    body = (
        "diff -u old/fs/foo.c new/fs/foo.c\n"
        "--- old/fs/foo.c\t2024-06-01 10:00:00.000000000 +0200\n"
        "+++ new/fs/foo.c\t2024-06-02 11:30:00.000000000 +0200\n"
        "@@ -1 +1 @@\n"
        "-a\n+b\n"
        "--- fs/bar.c\t2024-06-01 10:00:00 +0200\n"
        "+++ fs/bar.c\t2024-06-02 11:30:00 +0200\n"
        "@@ -5,2 +5,3 @@\n"
        " x\n+y\n"
        # An svn `Index:` block, read through its header pair.
        "Index: drivers/net/foo.c\n"
        "===================================================================\n"
        "--- drivers/net/foo.c\t(revision 1234)\n"
        "+++ drivers/net/foo.c\t(working copy)\n"
        "@@ -20,6 +20,7 @@\n"
        " a\n+b\n"
    )
    assert extract_touched_paths(body) == {
        "new/fs/foo.c",
        "fs/bar.c",
        "drivers/net/foo.c",
    }


def test_extract_unified_new_and_deleted_files():
    """`/dev/null` on one side: the new file's path, and for a deleted
    file the old path, as the git branch reports a deletion."""
    body = (
        "--- /dev/null\n"
        "+++ b/docs/new.rst\n"
        "@@ -0,0 +1,2 @@\n"
        "+one\n+two\n"
        "--- a/docs/gone.rst\n"
        "+++ /dev/null\n"
        "@@ -1,2 +0,0 @@\n"
        "-one\n-two\n"
    )
    assert extract_touched_paths(body) == {"docs/new.rst", "docs/gone.rst"}


def test_extract_git_diff_counts_each_file_once():
    """The `---` / `+++` pair inside a git diff names the path the
    `diff --git` header already gave; a rename still reports `b/`."""
    body = (
        "diff --git a/old.c b/new.c\n"
        "similarity index 90%\n"
        "rename from old.c\n"
        "rename to new.c\n"
        "--- a/old.c\n"
        "+++ b/new.c\n"
        "@@ -1 +1 @@\n"
        "-a\n+b\n"
        "diff --git a/gone.c b/gone.c\n"
        "deleted file mode 100644\n"
        "--- a/gone.c\n"
        "+++ /dev/null\n"
        "@@ -1 +0,0 @@\n"
        "-a\n"
    )
    assert extract_touched_paths(body) == {"new.c", "gone.c"}


def test_extract_unified_needs_a_hunk_header_at_column_zero():
    """A bare `---` signature separator, a header pair with no hunk, and
    a quoted diff name no file."""
    signature = "Thanks,\nA\n---\nfoo.c | 2 +-\n"
    no_hunk = "--- a/foo.c\n+++ b/foo.c\nsee the thread for the rest\n"
    quoted = (
        "On Mon, B wrote:\n> --- a/foo.c\n> +++ b/foo.c\n> @@ -1 +1 @@\n> -a\n> +b\n"
    )
    for body in (signature, no_hunk, quoted):
        assert extract_touched_paths(body) == set(), body


def test_extract_unified_is_linear_on_a_long_header_pair_without_a_hunk():
    """A hostile sender must not stall ingest: the header regex used to
    backtrack cubically on a long `---` / `+++` pair that no hunk header
    follows."""
    import time

    body = "--- " + "x" * 100_000 + "\n+++ " + "y" * 100_000 + "\nno hunk\n"
    started = time.monotonic()
    assert extract_touched_paths(body) == set()
    assert time.monotonic() - started < 1.0


def test_extract_unified_paths_with_spaces_and_crlf():
    spaced = "--- a/dir/my file.c\n+++ b/dir/my file.c\n@@ -1 +1 @@\n-a\n+b\n"
    crlf = "--- a/x.c\r\n+++ b/x.c\r\n@@ -1 +1 @@\r\n-a\r\n+b\r\n"
    assert extract_touched_paths(spaced) == {"dir/my file.c"}
    assert extract_touched_paths(crlf) == {"x.c"}
