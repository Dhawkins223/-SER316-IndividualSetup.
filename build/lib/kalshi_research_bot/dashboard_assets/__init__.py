"""Static assets for the research dashboard.

The stylesheet and script live as real files rather than Python string
constants so they can be linted, formatted, and diffed like the code they are.
They are read once at import time and served from hashed URLs, which lets the
browser cache them hard while the HTML itself stays `no-store`.
"""

from __future__ import annotations

import gzip
import hashlib
from pathlib import Path

ASSET_ROOT = Path(__file__).resolve().parent

# Media types for everything this package serves.
ASSET_CONTENT_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".woff2": "font/woff2",
}

# woff2 carries its own compression, so gzipping it again spends CPU to make
# the response slightly larger.
COMPRESSIBLE_SUFFIXES = frozenset({".css", ".js"})


def _read(name: str) -> bytes:
    return (ASSET_ROOT / name).read_bytes()


def _fingerprint(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()[:12]


class Asset:
    """One served file, addressed by a URL that changes when its bytes do."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.content_type = ASSET_CONTENT_TYPES[Path(name).suffix]
        self.adopt_body(_read(name))

    def adopt_body(self, payload: bytes) -> None:
        """Point the asset at new bytes, re-deriving everything keyed to them.

        The stylesheet's bytes change after construction -- font URLs are
        substituted in -- and both the fingerprint and the cached compression
        describe the body exactly. Going through one method keeps them from
        drifting out of step with it.
        """
        self.body = payload
        self.fingerprint = _fingerprint(payload)
        self._gzipped: bytes | None = None
        self._gzip_computed = False

    @property
    def gzipped(self) -> bytes | None:
        """The body pre-compressed, or None when compression would not help.

        Static bodies never change at runtime, so this is paid once at first
        use rather than on every request, which is why it can afford level 9.

        The server is threaded, so the flag is published *after* the bytes it
        describes. Setting it first let a second request in during the
        compression, see a finished-looking flag, and send the body
        uncompressed. The cost of this order is that simultaneous first
        requests may both compress; since the input is fixed and gzip is
        deterministic they produce the same bytes, so the duplicate work is
        wasted but never wrong.
        """
        if not self._gzip_computed:
            if Path(self.name).suffix in COMPRESSIBLE_SUFFIXES:
                packed = gzip.compress(self.body, 9)
                # A file small or dense enough to grow is served as it is.
                self._gzipped = packed if len(packed) < len(self.body) else None
            self._gzip_computed = True
        return self._gzipped

    @property
    def url(self) -> str:
        stem = Path(self.name).stem
        suffix = Path(self.name).suffix
        return f"/assets/{stem}.{self.fingerprint}{suffix}"


STYLESHEET = Asset("app.css")
SCRIPT = Asset("app.js")
LOGIN_SCRIPT = Asset("login.js")
OPS_SCRIPT = Asset("ops.js")
FONT_LATIN = Asset("inter-latin.woff2")
FONT_LATIN_EXT = Asset("inter-latin-ext.woff2")

_ALL = (STYLESHEET, SCRIPT, LOGIN_SCRIPT, OPS_SCRIPT, FONT_LATIN, FONT_LATIN_EXT)
ASSETS = {asset.url: asset for asset in _ALL}


def stylesheet_css() -> str:
    """The stylesheet with font URLs resolved to their hashed paths."""
    css = STYLESHEET.body.decode("utf-8")
    return css.replace("__INTER_LATIN__", FONT_LATIN.url).replace(
        "__INTER_LATIN_EXT__", FONT_LATIN_EXT.url
    )


# The font URLs are substituted into the CSS, so the stylesheet a browser
# receives differs from the file on disk; fingerprint what is actually sent.
STYLESHEET.adopt_body(stylesheet_css().encode("utf-8"))
ASSETS = {asset.url: asset for asset in _ALL}


def lookup(path: str) -> Asset | None:
    return ASSETS.get(path)
