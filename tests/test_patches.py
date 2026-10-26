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
    assert set(extract_touched_paths(body)) == set()


def test_extract_returns_empty_for_none_or_empty():
    assert set(extract_touched_paths(None)) == set()
    assert set(extract_touched_paths("")) == set()


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
    assert set(extract_touched_paths(body)) == {"fs/btrfs/extent_io.c"}


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
    assert set(extract_touched_paths(body)) == {
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
    paths = set(extract_touched_paths(body))
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
    assert set(extract_touched_paths(body)) == set()


def test_extract_handles_binary_diff_header():
    """Binary diffs still emit a `diff --git` header, capture them
    even though the hunk body is missing. Files matter for the
    subsystem mapping regardless of binary-ness."""
    body = (
        "diff --git a/firmware/blob.bin b/firmware/blob.bin\n"
        "Binary files a/firmware/blob.bin and b/firmware/blob.bin differ\n"
    )
    assert set(extract_touched_paths(body)) == {"firmware/blob.bin"}


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
    assert set(extract_touched_paths(body)) == {"fs/foo/a.c"}


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
    assert set(extract_touched_paths(body)) == {"net/core/frob.c"}


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
    assert set(extract_touched_paths(body)) == {
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
    assert set(extract_touched_paths(body)) == {"docs/new.rst", "docs/gone.rst"}


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
    assert set(extract_touched_paths(body)) == {"new.c", "gone.c"}


def test_extract_unified_needs_a_hunk_header_at_column_zero():
    """A bare `---` signature separator, a header pair with no hunk, and
    a quoted diff name no file."""
    signature = "Thanks,\nA\n---\nfoo.c | 2 +-\n"
    no_hunk = "--- a/foo.c\n+++ b/foo.c\nsee the thread for the rest\n"
    quoted = (
        "On Mon, B wrote:\n> --- a/foo.c\n> +++ b/foo.c\n> @@ -1 +1 @@\n> -a\n> +b\n"
    )
    for body in (signature, no_hunk, quoted):
        assert set(extract_touched_paths(body)) == set(), body


def test_extract_unified_is_linear_on_a_long_header_pair_without_a_hunk():
    """A hostile sender must not stall ingest: the header regex used to
    backtrack cubically on a long `---` / `+++` pair that no hunk header
    follows."""
    import time

    body = "--- " + "x" * 100_000 + "\n+++ " + "y" * 100_000 + "\nno hunk\n"
    started = time.monotonic()
    assert set(extract_touched_paths(body)) == set()
    assert time.monotonic() - started < 1.0


def test_extract_unified_paths_with_spaces_and_crlf():
    spaced = "--- a/dir/my file.c\n+++ b/dir/my file.c\n@@ -1 +1 @@\n-a\n+b\n"
    crlf = "--- a/x.c\r\n+++ b/x.c\r\n@@ -1 +1 @@\r\n-a\r\n+b\r\n"
    assert set(extract_touched_paths(spaced)) == {"dir/my file.c"}
    assert set(extract_touched_paths(crlf)) == {"x.c"}


# Per-file line counts (#939): `(insertions, deletions)` per path, read
# from the diff hunks, or None when a hunk is malformed.


def test_counts_each_file_from_its_own_hunks():
    body = (
        "diff --git a/fs/a.c b/fs/a.c\n"
        "index 1111111..2222222 100644\n"
        "--- a/fs/a.c\n"
        "+++ b/fs/a.c\n"
        "@@ -1,3 +1,4 @@ static int a(void)\n"
        " keep\n-old\n+new\n+more\n keep\n"
        "@@ -10,2 +11 @@\n"
        "-gone\n keep\n"
        "diff --git a/fs/b.c b/fs/b.c\n"
        "--- a/fs/b.c\n"
        "+++ b/fs/b.c\n"
        "@@ -1 +1,2 @@\n"
        " keep\n+added\n"
    )
    assert extract_touched_paths(body) == {"fs/a.c": (2, 2), "fs/b.c": (1, 0)}


def test_counts_stop_where_the_hunk_header_says():
    """The `-- ` signature separator and git's version line follow the
    last hunk. Only the header's line counts tell them apart from a
    deleted line, and a line inside a hunk that starts `---` is a
    deleted line, not a file header."""
    signature = (
        "diff --git a/f.c b/f.c\n--- a/f.c\n+++ b/f.c\n"
        "@@ -1 +1 @@\n-a\n+b\n-- \n2.43.0\n\n"
    )
    dash_line = "diff --git a/f.c b/f.c\n@@ -1,2 +1 @@\n--- x\n-- y\n+z\n"
    assert extract_touched_paths(signature) == {"f.c": (1, 1)}
    assert extract_touched_paths(dash_line) == {"f.c": (1, 2)}


def test_files_without_a_hunk_count_zero():
    """Binary files, a pure rename and a mode change touch the file and
    change no text line, as `git diff --shortstat` counts them."""
    body = (
        "diff --git a/fw.bin b/fw.bin\n"
        "Binary files a/fw.bin and b/fw.bin differ\n"
        "diff --git a/img.png b/img.png\n"
        "new file mode 100644\n"
        "index 0000000..1234567\n"
        "GIT binary patch\n"
        "literal 12\n"
        "Tcmb=-@@d(G%Sd&\n"
        "\n"
        "literal 0\n"
        "HcmV?d00001\n"
        "\n"
        "diff --git a/old.c b/new.c\n"
        "similarity index 100%\n"
        "rename from old.c\n"
        "rename to new.c\n"
        "diff --git a/run.sh b/run.sh\n"
        "old mode 100644\n"
        "new mode 100755\n"
    )
    assert extract_touched_paths(body) == {
        "fw.bin": (0, 0),
        "img.png": (0, 0),
        "new.c": (0, 0),
        "run.sh": (0, 0),
    }


def test_counts_deleted_and_new_files_and_unified_diffs():
    """A deleted file counts under its old path. Quilt, `diff -u` and
    `Index:` blocks count like git's."""
    body = (
        "diff --git a/gone.c b/gone.c\n"
        "deleted file mode 100644\n"
        "--- a/gone.c\n"
        "+++ /dev/null\n"
        "@@ -1,2 +0,0 @@\n"
        "-a\n-b\n"
        "--- a/net/x.c\n"
        "+++ b/net/x.c\n"
        "@@ -1,2 +1,2 @@\n"
        " ctx\n-a\n+b\n"
        "--- /dev/null\n"
        "+++ b/docs/new.rst\n"
        "@@ -0,0 +1,3 @@\n"
        "+one\n+two\n+three\n"
        "Index: drivers/net/foo.c\n"
        "===================================================================\n"
        "--- drivers/net/foo.c\t(revision 1234)\n"
        "+++ drivers/net/foo.c\t(working copy)\n"
        "@@ -20,2 +20,3 @@\n"
        " a\n+b\n c\n"
    )
    assert extract_touched_paths(body) == {
        "gone.c": (0, 2),
        "net/x.c": (1, 1),
        "docs/new.rst": (3, 0),
        "drivers/net/foo.c": (1, 0),
    }


def test_counts_survive_mail_damage_git_apply_accepts():
    """A mailer that strips trailing spaces turns an empty context line
    into an empty line, and CRLF mail ends every line in `\\r`."""
    stripped = "diff --git a/f.c b/f.c\n@@ -1,3 +1,3 @@\n a\n\n-b\n+c\n"
    crlf = "--- a/x.c\r\n+++ b/x.c\r\n@@ -1 +1 @@\r\n-a\r\n+b\r\n"
    no_newline = (
        "diff --git a/f.c b/f.c\n@@ -1 +1 @@\n-a\n\\ No newline at end of file\n+b\n"
    )
    assert extract_touched_paths(stripped) == {"f.c": (1, 1)}
    assert extract_touched_paths(crlf) == {"x.c": (1, 1)}
    assert extract_touched_paths(no_newline) == {"f.c": (1, 1)}


def test_a_malformed_hunk_leaves_its_file_uncounted():
    """A partial count would be a wrong number shown as a fact, so the
    file gets None. The next file's count is unaffected, including when
    the broken hunk runs into that file's header."""
    for broken in (
        "@@ -1,5 +1,5 @@\n-a\n+b\n",  # ends early
        "@@ -1,2 +1,2 @@\n-a\nwrapped by the mailer\n+b\n ctx\n",  # stray line
        "@@ -1 +1 @@\n-a\n-b\n+c\n",  # more deletions than the header holds
        "@@ -1,999999999 +1,999999999 @@\n-a\n",  # absurd count
        "@@ -1,1234567890 +1 @@\n-a\n+b\n",  # a count past nine digits
        "@@@ -1,2 -1,2 +1,3 @@@\n",  # a combined diff's header
    ):
        body = (
            "diff --git a/bad.c b/bad.c\n"
            + broken
            + "diff --git a/good.c b/good.c\n@@ -1 +1 @@\n-a\n+b\n"
        )
        assert extract_touched_paths(body) == {"bad.c": None, "good.c": (1, 1)}, broken


def test_counts_sum_a_repeated_path_and_skip_a_headerless_hunk():
    body = (
        "Pasted without a header:\n"
        "@@ -1 +1 @@\n-x\n+y\n"
        "diff --git a/fs/foo/a.c b/fs/foo/a.c\n"
        "@@ -1 +1 @@\n-x\n+y\n"
        "diff --git a/fs/foo/a.c b/fs/foo/a.c\n"
        "@@ -2 +2,2 @@\n-x\n+y\n+z\n"
    )
    assert extract_touched_paths(body) == {"fs/foo/a.c": (3, 2)}


def test_a_capped_path_s_hunks_are_not_credited_to_the_file_before_it():
    from mimir.patches import MAX_TOUCHED_PATHS

    body = "".join(
        f"diff --git a/f{i}.c b/f{i}.c\n@@ -1 +1 @@\n-a\n+b\n"
        for i in range(MAX_TOUCHED_PATHS + 1)
    )
    counts = extract_touched_paths(body)
    assert len(counts) == MAX_TOUCHED_PATHS
    assert f"f{MAX_TOUCHED_PATHS}.c" not in counts  # precondition: one was capped
    assert set(counts.values()) == {(1, 1)}


def test_counting_is_linear_on_hostile_hunks():
    """AGENTS.md check 7: every hunk line is read once, whatever a
    sender wrote. A count past nine digits never reaches `int()`, which
    refuses strings over 4300 digits."""
    import time

    n = 300_000
    git = "diff --git a/f b/f\n"
    cases = [
        (git + "@@ -1,999999999 +1,999999999 @@\n" + "+x\n" * n, {"f": None}),
        (git + "@@ -1 +1 @@\n-a\n+b\n" * n, {"f": (n, n)}),
        ((git + "--- a/f\n+++ b/f\n@@ -1 +1 @@\n-a\n+b\n") * n, {"f": (n, n)}),
        (git + "@@ -1," + "1" * n + " +1 @@\n-a\n+b\n", {"f": None}),
    ]
    for body, expected in cases:
        started = time.monotonic()
        assert extract_touched_paths(body) == expected
        assert time.monotonic() - started < 5, body[:60]
    started = time.monotonic()
    many = "".join(f"diff --git a/f{i} b/f{i}\n@@ -1 +1 @@\n-a\n+b\n" for i in range(n))
    assert len(extract_touched_paths(many)) == 1000
    assert time.monotonic() - started < 5
