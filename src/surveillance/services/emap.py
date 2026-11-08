# Copyright (c) 2026, Renaud Allard <renaud@allard.it>
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice,
#    this list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

"""Surveillance Station E-Maps: reading them, and editing them.

An E-Map is an image, typically a floor plan, with cameras placed on it.
As Surveillance Station 9.3 answers them:

- ``SYNO.SurveillanceStation.Emap`` v1 ``List``: ``data.emap`` holds every
  map with its ``id``, ``name``, image ``filename``, ``imgWidth`` and
  ``imgHeight``, but not its items.
- ``SYNO.SurveillanceStation.Emap`` v2 ``Load`` with ``emapIds``: the same
  object, as ``data.emap[0]``, with ``item`` filled in. A camera marker has
  ``refItemType`` 0 and the camera id in ``refItemIdOnHost``.
- ``SYNO.SurveillanceStation.Emap.Image`` v1 ``Load`` with ``filename``:
  the image bytes (PNG, JPEG or SVG).
- ``SYNO.SurveillanceStation.Emap.Image`` v1 ``Upload``: a multipart POST
  with one field, ``emapImg`` (a PNG), answered with ``data.filename``,
  ``imgWidth``, ``imgHeight``, ``imageData`` and ``imageSize``. The NAS
  stores the image under that ``filename``, not the local one.
- ``SYNO.SurveillanceStation.Emap`` v2 ``Save``: with ``emapIds`` 0 and a
  ``name`` plus the Upload answer as ``imgName``, ``imgWidth``,
  ``imgHeight``, ``imgData`` and ``byteSize`` it creates a map and answers
  ``data.addEmapId``; with an existing id and the same image fields it
  swaps the map's image and keeps its items; with an existing id and
  ``items`` (``{"list": [...]}``) it REPLACES the map's whole item list,
  so every item the map should keep has to be in it.
- ``SYNO.SurveillanceStation.Emap`` v1 ``Delete`` with ``emapIds``: removes
  the map.

String parameters go JSON-encoded, quotes included, the way Surveillance
Station's own web UI sends them. Save goes as a POST: its image data and
item list do not fit a URL.

Marker coordinates are fractions of the image HEIGHT on both axes, with
the origin at the bottom-left: ``lng = x / H`` and ``lat = (H - y) / H`` for
a pixel (x, y) counted from the top-left of an image H pixels high.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from surveillance.api.auth import SessionExpiredError
from surveillance.api.client import ApiError
from surveillance.api.models import EMap, EMapItem

if TYPE_CHECKING:
    from surveillance.api.client import SurveillanceAPI

EMAP_API = "SYNO.SurveillanceStation.Emap"
EMAP_IMAGE_API = "SYNO.SurveillanceStation.Emap.Image"
# The one form field of an Emap.Image Upload.
UPLOAD_FIELD = "emapImg"

# 102-104: no such API, method or version; 105 even after the fresh login
# request() makes for it: the account may not see E-Maps.
_UNAVAILABLE_CODES = (102, 103, 104, 105)

# The only Emap Load whose items this client reads (refItemIdOnHost, lng,
# lat). request() lowers a version the NAS does not offer, and the v1
# Load the API guide documents answers with uniqueId, x and y instead, so
# every marker would be dropped without a word.
_LOAD_VERSION = 2


class EMapsTooOldError(Exception):
    """The NAS's Emap API stops before the Load this client reads."""


def emaps_unavailable(error: BaseException) -> bool:
    """True when *error* means this NAS or account has no E-Maps at all,
    rather than one call failing. A failed re-login counts too, so the page
    does not log in again on every visit; the next connect starts over."""
    if isinstance(error, (SessionExpiredError, EMapsTooOldError)):
        return True
    return isinstance(error, ApiError) and error.code in _UNAVAILABLE_CODES


def emaps_read_only(error: BaseException) -> bool:
    """True when an edit failed because the account may view E-Maps but
    not change them: a Spectator gets 105 from Save, Upload and Delete
    while List and Load keep working."""
    return isinstance(error, ApiError) and error.code == 105


class DuplicateMapNameError(ValueError):
    """A map of that name exists already. The NAS would accept a second
    one; this client does not, since the list would then show two
    indistinguishable rows."""


class StaleMapError(RuntimeError):
    """The map's item list on the NAS is not the one the edits started
    from, so saving them would overwrite someone else's changes."""


@dataclass(frozen=True)
class UploadedImage:
    """What Emap.Image Upload answered, to hand to create_map or
    replace_image as the map's image."""

    filename: str
    width: int
    height: int
    image_data: str
    byte_size: int

    @classmethod
    def from_api(cls, data: Any) -> UploadedImage:
        """Raises ValueError on an answer without a stored filename."""
        if not isinstance(data, dict) or not data.get("filename"):
            raise ValueError("The NAS did not name the uploaded image")
        return cls(
            filename=str(data["filename"]),
            width=int(data.get("imgWidth", 0)),
            height=int(data.get("imgHeight", 0)),
            image_data=str(data.get("imageData", "")),
            byte_size=int(data.get("imageSize", 0)),
        )

    def save_params(self) -> dict[str, Any]:
        """The image half of an Emap Save."""
        return {
            "imgName": json.dumps(self.filename),
            "imgWidth": self.width,
            "imgHeight": self.height,
            "imgData": json.dumps(self.image_data),
            "byteSize": self.byte_size,
        }


def marker_to_pixels(lng: float, lat: float, image_height: float) -> tuple[float, float]:
    """A marker's position in image pixels, from the top-left.

    Both coordinates scale with the image height, so lng is not clamped:
    it runs to width / height on a landscape image.
    """
    return lng * image_height, image_height - lat * image_height


def pixels_to_marker(x: float, y: float, image_height: float) -> tuple[float, float]:
    """The (lng, lat) Surveillance Station stores for image pixel (x, y)."""
    return x / image_height, (image_height - y) / image_height


def parse_maps(data: Any) -> list[EMap]:
    """Maps from an Emap List answer, rows without an id skipped."""
    rows = data.get("emap", []) if isinstance(data, dict) else []
    maps: list[EMap] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        try:
            emap = EMap.from_api(row)
        except (TypeError, ValueError):
            continue
        if emap.id > 0:
            maps.append(emap)
    return maps


def parse_map(data: Any) -> EMap | None:
    """The one map of an Emap Load answer, None when it holds none."""
    maps = parse_maps(data)
    return maps[0] if maps else None


class EMapService:
    """E-Map reads for one session, with what they return cached.

    Build one per login: map ids and image names belong to one NAS.
    """

    def __init__(self, api: SurveillanceAPI) -> None:
        self.api = api
        self._maps: dict[int, EMap] = {}
        self._images: dict[str, bytes] = {}

    def clear(self) -> None:
        """Forget the cached maps and images, so the next reads ask again."""
        self._maps.clear()
        self._images.clear()

    async def list_maps(self) -> list[EMap]:
        """Every map, without its items. Raises EMapsTooOldError, before
        asking anything, when the NAS cannot load a map's items."""
        version = self.api._get_api_version(EMAP_API, _LOAD_VERSION)
        if version < _LOAD_VERSION:
            raise EMapsTooOldError(f"{EMAP_API} goes up to version {version} only")
        data = await self.api.request(api=EMAP_API, method="List", version=1)
        return parse_maps(data)

    async def load_map(self, emap_id: int) -> EMap | None:
        """One map with its items, None when the NAS has no such map."""
        cached = self._maps.get(emap_id)
        if cached is not None:
            return cached
        data = await self.api.request(
            api=EMAP_API,
            method="Load",
            version=_LOAD_VERSION,
            extra_params={"emapIds": json.dumps(str(emap_id))},
        )
        emap = parse_map(data)
        if emap is not None:
            self._maps[emap_id] = emap
        return emap

    async def load_image(self, image_name: str) -> bytes:
        """The bytes of a map's image, as named by EMap.image_name."""
        cached = self._images.get(image_name)
        if cached is not None:
            return cached
        content = await self.api.download(
            api=EMAP_IMAGE_API,
            method="Load",
            version=1,
            extra_params={"filename": json.dumps(image_name)},
        )
        self._images[image_name] = content
        return content

    async def load_map_with_image(self, emap_id: int) -> tuple[EMap | None, bytes | None]:
        """A map and its image; the image is None when the map has none."""
        emap = await self.load_map(emap_id)
        if emap is None or not emap.image_name:
            return emap, None
        return emap, await self.load_image(emap.image_name)

    # -- editing -------------------------------------------------------

    async def upload_image(self, png_bytes: bytes, filename: str) -> UploadedImage:
        """Store a PNG on the NAS for a map to use. *filename* is only a
        hint; the NAS answers with the name it stored the image under."""
        data = await self.api.upload(
            api=EMAP_IMAGE_API,
            method="Upload",
            version=1,
            field=UPLOAD_FIELD,
            filename=filename,
            content=png_bytes,
            content_type="image/png",
        )
        return UploadedImage.from_api(data)

    async def _save(self, emap_id: int, **params: Any) -> Any:
        params["emapIds"] = json.dumps(str(emap_id))
        return await self.api.request(
            api=EMAP_API, method="Save", version=2, extra_params=params, post=True
        )

    async def create_map(self, name: str, uploaded: UploadedImage) -> int:
        """A new map named *name* with the uploaded image, by its id.

        Raises DuplicateMapNameError when a map of that name is listed.
        """
        if any(m.name.casefold() == name.casefold() for m in await self.list_maps()):
            raise DuplicateMapNameError(f"An E-Map named {name!r} exists already")
        data = await self._save(0, name=json.dumps(name), **uploaded.save_params())
        try:
            new_id = int(data["addEmapId"])
        except (KeyError, TypeError, ValueError) as e:
            raise ApiError(100, "The NAS did not return the new E-Map's id") from e
        return new_id

    async def replace_image(self, emap_id: int, uploaded: UploadedImage) -> None:
        """Give an existing map the uploaded image; its items stay."""
        await self._save(emap_id, **uploaded.save_params())
        cached = self._maps.pop(emap_id, None)
        if cached is not None:
            self._images.pop(cached.image_name, None)

    async def save_markers(
        self, emap_id: int, items: Iterable[EMapItem], base_item_ids: Collection[int]
    ) -> None:
        """Make *items* the map's whole item list.

        *base_item_ids* are the ids of the items the edits started from.
        The map is reloaded first, and when its items are not those any
        more the save is refused with StaleMapError: Save replaces the
        list, so it would silently drop whatever was placed since. Rows
        of that reload this client could not model go back as they came.
        """
        self._maps.pop(emap_id, None)
        current = await self.load_map(emap_id)
        if current is None:
            raise StaleMapError("This E-Map no longer exists on the NAS")
        if {item.id for item in current.items} != set(base_item_ids):
            raise StaleMapError(
                "This E-Map's markers changed on the NAS since it was loaded; "
                "reload it and make the changes again"
            )
        payload = {"list": [item.to_api(emap_id) for item in items] + list(current.extra_rows)}
        await self._save(emap_id, items=json.dumps(payload))
        self._maps.pop(emap_id, None)

    async def delete_map(self, emap_id: int) -> None:
        """Remove a map from the NAS (Emap v1 Delete)."""
        await self.api.request(
            api=EMAP_API,
            method="Delete",
            version=1,
            extra_params={"emapIds": json.dumps(str(emap_id))},
        )
        cached = self._maps.pop(emap_id, None)
        if cached is not None:
            self._images.pop(cached.image_name, None)
