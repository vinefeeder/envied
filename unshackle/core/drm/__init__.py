from typing import Union

from unshackle.core.drm.clearkey import ClearKey
from unshackle.core.drm.playready import PlayReady
from unshackle.core.drm.widevine import Widevine

DRM_T = Union[ClearKey, Widevine, PlayReady]


__all__ = ("ClearKey", "Widevine", "PlayReady", "DRM_T")
