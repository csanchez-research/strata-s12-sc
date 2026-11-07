"""The real cause of a failure reaches the log, without access tokens."""

from __future__ import annotations

from strata_complete.runtime import errors


def _gdal_read() -> None:
    msg = "TIFFFillTile: got 31872 bytes, expected 99855 /vsicurl/https://x.net/a.tif?sig=SECRET"
    raise OSError(msg)


def _rasterio_read() -> None:
    try:
        _gdal_read()
    except OSError as inner:
        outer = "Read failed. See previous exception for details."
        raise RuntimeError(outer) from inner


def test_describe_keeps_the_chain_and_drops_tokens() -> None:
    try:
        _rasterio_read()
    except RuntimeError as exc:
        line = errors.describe(exc)
    assert line.startswith("RuntimeError: Read failed")
    assert "got 31872 bytes, expected 99855" in line
    assert "SECRET" not in line
    assert "https://x.net/a.tif?…" in line
