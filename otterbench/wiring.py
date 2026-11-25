"""What is connected to what: a note and a photo of the bench as it is now.

Kept in ~/.config/otterbench/wiring.json, in every setup saved, in the session
being recorded, and shown in reports.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
from dataclasses import dataclass

from .config import config_dir

MAX_PHOTO = 3_000_000            # bytes of JPEG / PNG
MAX_TEXT = 20_000


@dataclass
class Wiring:
    text: str = ""
    photo: str = ""              # base64 of a JPEG or PNG

    @property
    def empty(self) -> bool:
        return not self.text.strip() and not self.photo

    def to_dict(self) -> dict:
        return {"text": self.text, "photo": self.photo}

    @classmethod
    def from_dict(cls, d) -> "Wiring":
        """Anything unusable in d (a damaged or foreign file) is left out."""
        if not isinstance(d, dict):
            return cls()
        photo = d.get("photo")
        photo = photo if isinstance(photo, str) and image_type(decode(photo)) else ""
        text = d.get("text")
        return cls(text[:MAX_TEXT] if isinstance(text, str) else "", photo)

    @property
    def image(self) -> bytes:
        return decode(self.photo)


def decode(text: str) -> bytes:
    try:
        data = base64.b64decode(text or "", validate=True)
    except (binascii.Error, ValueError):
        return b""
    return data if len(data) <= MAX_PHOTO else b""


def image_type(data: bytes) -> str:
    """The MIME type by the first bytes ("" = not a picture we show)."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"BM"):
        return "image/bmp"
    return ""


def data_uri(data: bytes) -> str:
    kind = image_type(data)
    return f"data:{kind};base64,{base64.b64encode(data).decode()}" if kind else ""


def path():
    return config_dir() / "wiring.json"


_current: Wiring | None = None


def current() -> Wiring:
    global _current
    if _current is None:
        try:
            _current = Wiring.from_dict(json.loads(path().read_text(encoding="utf-8")))
        except (OSError, ValueError):
            _current = Wiring()
    return _current


def set_current(w: Wiring):
    """Remember it; what was there before is kept in wiring.json.old."""
    global _current
    old, _current = current(), w
    if old == w:
        return
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    if not old.empty and p.exists():
        os.replace(p, p.with_suffix(".json.old"))
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(w.to_dict()), encoding="utf-8")
    os.replace(tmp, p)
