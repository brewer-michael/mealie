"""
Card images as AI request attachments (docs/ai/PHASE2.md §4.1, F16).

Upstream's `OpenAILocalImage` re-encodes its file on every provider attempt and writes `<name>-min-original.jpg` beside
it. A card's `view.jpg` is already an EXIF-free JPEG of at most 2048 px, so `CardImage` sends it as it is: it reads the
file once, keeps the data URL for later attempts, and writes nothing.
"""

import base64
from pathlib import Path

from pydantic import PrivateAttr

from mealie.services.openai.openai import OpenAIImageBase


class CardImage(OpenAIImageBase):
    """A JPEG from a card page's directory (`path`), or one built in memory (`jpeg`, a region re-read's crop)"""

    path: Path | None = None
    jpeg: bytes | None = None

    _data_url: str | None = PrivateAttr(default=None)

    def get_image_url(self) -> str:
        if self._data_url is None:
            if self.jpeg is not None:
                data = self.jpeg
            elif self.path is not None:
                data = self.path.read_bytes()
            else:
                raise ValueError("A card image needs a path or JPEG bytes")
            self._data_url = f"data:image/jpeg;base64,{base64.b64encode(data).decode('ascii')}"
        return self._data_url
