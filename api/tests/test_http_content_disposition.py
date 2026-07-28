"""The shared attachment ``Content-Disposition`` builder (issue #2357).

The names reaching this helper are attacker-influenced (a server name, an
uploaded resource-pack name, a working-set path segment), and each hostile shape
has a different failure mode -- header injection, a 500 on encoding, a saved file
escaping the download directory -- so every one is pinned here.
"""

from __future__ import annotations

from mc_server_dashboard_api.http_content_disposition import content_disposition


def test_plain_name_is_carried_in_both_parameters() -> None:
    assert (
        content_disposition("world.zip")
        == "attachment; filename=\"world.zip\"; filename*=UTF-8''world.zip"
    )


def test_quote_cannot_inject_extra_parameters() -> None:
    # An embedded " would close the quoted-string and let the rest of the name be
    # read as further disposition parameters.
    cd = content_disposition('evil".zip')
    assert cd == "attachment; filename=\"evil_.zip\"; filename*=UTF-8''evil%22.zip"


def test_crlf_cannot_split_the_header() -> None:
    cd = content_disposition("a\r\nX-Injected: 1.zip")
    assert "\r" not in cd and "\n" not in cd
    assert 'filename="a__X-Injected: 1.zip"' in cd


def test_non_ascii_name_survives_in_filename_star() -> None:
    # Starlette latin-1-encodes headers, so the raw kana would 500; they ride in
    # the percent-encoded filename* with an ASCII fallback for legacy clients.
    cd = content_disposition("ワールド.zip")
    assert 'filename="____.zip"' in cd
    assert "filename*=UTF-8''%E3%83%AF%E3%83%BC%E3%83%AB%E3%83%89.zip" in cd
    cd.encode("latin-1")


def test_directory_component_is_stripped() -> None:
    # A name is free-form, so it can carry path separators. Neither parameter may
    # emit them: filename* percent-encodes "/" but the client decodes it back, so
    # a traversal would reach whatever writes the file out.
    cd = content_disposition("../../etc/passwd.zip")
    assert cd == "attachment; filename=\"passwd.zip\"; filename*=UTF-8''passwd.zip"


def test_backslash_directory_component_is_stripped() -> None:
    # The saving client may be on Windows, where "\" separates too (and the ASCII
    # fallback's "_" substitution would not stop filename* from decoding it).
    cd = content_disposition(r"..\..\windows\system32\evil.zip")
    assert cd == "attachment; filename=\"evil.zip\"; filename*=UTF-8''evil.zip"


def test_name_that_is_only_a_path_falls_back_to_download() -> None:
    # "../" leaves no last segment to name the payload with.
    cd = content_disposition("../../")
    assert cd == "attachment; filename=\"download\"; filename*=UTF-8''download"


def test_blank_last_segment_falls_back_to_download() -> None:
    # A segment of only whitespace is as unusable as one of only dots, and would
    # otherwise name the payload " ".
    cd = content_disposition("foo/ ")
    assert cd == "attachment; filename=\"download\"; filename*=UTF-8''download"
