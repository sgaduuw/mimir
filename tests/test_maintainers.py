"""Pure-function parser tests for `mimir.maintainers`. The CLI
integration (clone/fetch/load) is exercised separately in
tests/test_cli_maintainers.py against a fake bare repo.
"""
from mimir.maintainers import (
    MaintainerEntry,
    Subsystem,
    _split_addr,
    iter_subsystem_paths,
    parse,
)


_PREAMBLE = (
    b"List of maintainers and how to submit kernel changes\n"
    b"====================================================\n"
    b"\n"
    b"...documentation...\n"
    b"\n"
    b"Description of section entries:\n"
    b"\n"
    b"    M: Mail patches to: FullName <address@domain>\n"
    b"    ...\n"
    b"\n"
    b"Maintainers List\n"
    b"----------------\n"
    b"\n"
)


def _section(body: str) -> bytes:
    """Wrap a section snippet in the preamble + a trailing blank
    line, mimicking the upstream file's shape."""
    return _PREAMBLE + body.encode("utf-8") + b"\n"


def test_parse_single_maintained_section():
    blob = _section(
        "BCACHEFS\n"
        "M:\tKent Overstreet <kent.overstreet@linux.dev>\n"
        "L:\tlinux-bcachefs@vger.kernel.org\n"
        "S:\tMaintained\n"
        "F:\tfs/bcachefs/\n"
    )
    out = parse(blob)
    assert len(out) == 1
    sub = out[0]
    assert sub.name == "BCACHEFS"
    assert sub.status == "Maintained"
    assert sub.maintainers == [
        MaintainerEntry(role="M", name="Kent Overstreet",
                        address="kent.overstreet@linux.dev"),
    ]
    assert sub.lists == ["linux-bcachefs@vger.kernel.org"]
    assert sub.files == ["fs/bcachefs/"]
    assert sub.excludes == []


def test_parse_multiple_maintainers_and_reviewers():
    """Both `M:` (maintainer) and `R:` (reviewer) entries land in
    `maintainers` with the corresponding `role`. Order is source-
    file order — preserves any author-intended hierarchy."""
    blob = _section(
        "EXAMPLE SUBSYSTEM\n"
        "M:\tA <a@example>\n"
        "M:\tB <b@example>\n"
        "R:\tC <c@example>\n"
        "S:\tSupported\n"
        "F:\tdrivers/example/\n"
    )
    sub = parse(blob)[0]
    assert [(m.role, m.address) for m in sub.maintainers] == [
        ("M", "a@example"), ("M", "b@example"), ("R", "c@example"),
    ]


def test_parse_handles_excludes_and_multiple_paths():
    blob = _section(
        "FOO\n"
        "M:\tA <a@example>\n"
        "S:\tMaintained\n"
        "F:\tfs/foo/\n"
        "F:\tinclude/uapi/linux/foo.h\n"
        "X:\tfs/foo/legacy/\n"
    )
    sub = parse(blob)[0]
    assert sub.files == ["fs/foo/", "include/uapi/linux/foo.h"]
    assert sub.excludes == ["fs/foo/legacy/"]


def test_parse_strips_parenthesised_list_qualifier():
    """Some `L:` lines append a parenthesised qualifier like
    `(moderated for non-subscribers)`. The canonical address — what
    we'd match against — is the bare email."""
    blob = _section(
        "FOO\n"
        "M:\tA <a@example>\n"
        "L:\tfoo@vger.kernel.org (moderated for non-subscribers)\n"
        "S:\tMaintained\n"
        "F:\tfs/foo/\n"
    )
    sub = parse(blob)[0]
    assert sub.lists == ["foo@vger.kernel.org"]


def test_parse_two_sections_separated_by_blank_line():
    blob = _section(
        "FIRST\n"
        "M:\tA <a@example>\n"
        "S:\tMaintained\n"
        "F:\tfirst/\n"
        "\n"
        "SECOND\n"
        "M:\tB <b@example>\n"
        "S:\tOdd Fixes\n"
        "F:\tsecond/\n"
    )
    out = parse(blob)
    assert [s.name for s in out] == ["FIRST", "SECOND"]
    assert out[1].status == "Odd Fixes"
    assert out[1].files == ["second/"]


def test_parse_ignores_unknown_tags():
    """Tags we don't model (`T:`, `W:`, `B:`, `K:`, etc.) must not
    crash or pollute the entry. They're skipped silently — the spec
    permits unrecognised tags."""
    blob = _section(
        "FOO\n"
        "M:\tA <a@example>\n"
        "T:\tgit https://example/foo.git\n"
        "W:\thttps://example/foo\n"
        "B:\thttps://example/bugs\n"
        "Q:\thttps://patchwork/foo\n"
        "K:\t^foo_\n"
        "S:\tMaintained\n"
        "F:\tfoo/\n"
    )
    sub = parse(blob)[0]
    assert len(sub.maintainers) == 1
    assert sub.files == ["foo/"]


def test_parse_skips_preamble_doc_text():
    """Lines before `Description of section entries:` must not be
    parsed as a subsystem section. The doc text contains lines that
    *look* like section titles ("List of maintainers and how to
    submit kernel changes")."""
    blob = (
        b"List of maintainers and how to submit kernel changes\n"
        b"====================================================\n"
        b"\n"
        b"Anything in this block must be invisible to the parser.\n"
        b"Random Title Looking Line\n"
        b"\n"
        b"Description of section entries:\n"
        b"\n"
        b"FOO\n"
        b"M:\tA <a@example>\n"
        b"S:\tMaintained\n"
        b"F:\tfoo/\n"
    )
    out = parse(blob)
    assert [s.name for s in out] == ["FOO"]


def test_parse_handles_missing_status_tag():
    """`status` is None when `S:` is omitted (rare but legal in
    older sections)."""
    blob = _section(
        "FOO\n"
        "M:\tA <a@example>\n"
        "F:\tfoo/\n"
    )
    sub = parse(blob)[0]
    assert sub.status is None


def test_parse_handles_parenthesised_qualifier_in_maintainer_name():
    """`Tony Luck (FREENAME) <tony.luck@intel.com>` should land as
    `name="Tony Luck"`, dropping the parenthesised qualifier. The
    qualifier is documentation, not part of the identity."""
    blob = _section(
        "FOO\n"
        "M:\tTony Luck (FREENAME) <tony.luck@intel.com>\n"
        "S:\tMaintained\n"
        "F:\tfoo/\n"
    )
    sub = parse(blob)[0]
    assert sub.maintainers[0].name == "Tony Luck"
    assert sub.maintainers[0].address == "tony.luck@intel.com"


def test_parse_drops_malformed_maintainer_lines():
    """A `M:` line with no `<addr>` part is malformed; the parser
    drops it instead of fabricating a half-row. Other valid lines
    in the same section still land."""
    blob = _section(
        "FOO\n"
        "M:\tno-angle-brackets-here\n"
        "M:\tValid <valid@example>\n"
        "S:\tMaintained\n"
        "F:\tfoo/\n"
    )
    sub = parse(blob)[0]
    assert len(sub.maintainers) == 1
    assert sub.maintainers[0].address == "valid@example"


def test_parse_handles_bare_address_maintainer():
    """`M:\t<bare@addr>` with no display name lands with name=''."""
    blob = _section(
        "FOO\n"
        "M:\t<bare@example>\n"
        "S:\tMaintained\n"
        "F:\tfoo/\n"
    )
    sub = parse(blob)[0]
    assert sub.maintainers[0].name == ""
    assert sub.maintainers[0].address == "bare@example"


def test_parse_handles_non_ascii_names_via_surrogateescape():
    """Non-decodable bytes survive parsing via surrogateescape, so
    a stray non-UTF-8 byte in a contributor name doesn't crash the
    entire file. The downstream consumer can re-encode if needed."""
    # A latin-1 byte (0xe9 = é) in a name — would normally raise
    # under strict UTF-8 decoding.
    blob = _section("FOO\nM:\tJos\xe9 Doe <j@example>\nS:\tMaintained\nF:\tfoo/\n")
    sub = parse(blob)[0]
    assert sub.maintainers[0].address == "j@example"


def test_iter_subsystem_paths_yields_includes_then_excludes():
    sub = Subsystem(
        name="FOO",
        files=["fs/foo/", "include/foo.h"],
        excludes=["fs/foo/legacy/"],
    )
    rows = list(iter_subsystem_paths([sub]))
    assert rows == [
        (sub, "fs/foo/", False),
        (sub, "include/foo.h", False),
        (sub, "fs/foo/legacy/", True),
    ]


def test_split_addr_handles_simple_form():
    assert _split_addr("Foo Bar <foo@bar>") == ("Foo Bar", "foo@bar")


def test_split_addr_handles_bare_address():
    assert _split_addr("<foo@bar>") == ("", "foo@bar")


def test_split_addr_rejects_no_angle_brackets():
    assert _split_addr("not an address") is None
