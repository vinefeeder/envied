import json
import re
from http.cookiejar import CookieJar
from typing import Optional
from langcodes import Language

import click

from unshackle.core.constants import AnyTrack
from unshackle.core.credential import Credential
from unshackle.core.manifests import DASH
from unshackle.core.service import Service
from unshackle.core.titles import Episode, Movie, Movies, Series, Title_T, Titles_T
from unshackle.core.tracks import Chapter, Tracks, Subtitle


class NPO(Service):
    """
    Service code for NPO Start (npo.nl)
    Version: 1.0.0

    Authorization: optional cookies (free/paid content supported)
    Security: FHD @ L3 (Widevine)

    Supports:
      • Series ↦ https://npo.nl/start/serie/{slug}
      • Movies ↦ https://npo.nl/start/video/{slug}
    
    Only supports widevine at the moment

    Note: Movie that is inside  in a series (e.g.
      https://npo.nl/start/serie/zappbios/.../zappbios-captain-nova/afspelen)
      can be downloaded as movies by converting the URL to:
      https://npo.nl/start/video/zappbios-captain-nova
    """

    TITLE_RE = (
        r"^(?:https?://(?:www\.)?npo\.nl/start/)?"
        r"(?:(?P<type>video|serie)/(?P<slug>[^/]+)"
        r"(?:/afleveringen)?"
        r"(?:/seizoen-(?P<season>[^/]+)/(?P<episode>[^/]+)/afspelen)?)?$"
    )
    GEOFENCE = ("NL",)
    NO_SUBTITLES = False

    @staticmethod
    @click.command(name="NPO", short_help="https://npo.nl")
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx, **kwargs):
        return NPO(ctx, **kwargs)

    def __init__(self, ctx, title: str):
        super().__init__(ctx)

        m = re.match(self.TITLE_RE, title)
        if not m:
            raise ValueError(
                f"Unsupported NPO URL: {title}\n"
                "Use /video/slug for movies or /serie/slug for series."
            )

        self.slug = m.group("slug")
        self.kind = m.group("type") or "video"
        self.season_slug = m.group("season")
        self.episode_slug = m.group("episode")

        if self.config is None:
            raise EnvironmentError("Missing service config.")

    def authenticate(self, cookies: Optional[CookieJar] = None, credential: Optional[Credential] = None) -> None:
        super().authenticate(cookies, credential)
        if not cookies:
            self.log.info("No cookies, proceeding anonymously.")
            return

        token = next((c.value for c in cookies if c.name == "__Secure-next-auth.session-token"), None)
        if not token:
            self.log.info("No session token, proceeding unauthenticated.")
            return

        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Firefox/143.0",
            "Origin": "https://npo.nl",
            "Referer": "https://npo.nl/",
        })

        r = self.session.get("https://npo.nl/start/api/domain/user-profiles", cookies=cookies)
        if r.ok and isinstance(r.json(), list) and r.json():
            self.log.info(f"NPO login OK, profiles: {[p['name'] for p in r.json()]}")
        else:
            self.log.warning("NPO auth check failed.")

    def _get_build_id(self, slug: str) -> str:
        """Fetch buildId from the actual video/series page."""
        url = f"https://npo.nl/start/{'video' if self.kind == 'video' else 'serie'}/{slug}"
        r = self.session.get(url)
        r.raise_for_status()
        match = re.search(r'<script id="__NEXT_DATA__" type="application/json">({.*?})</script>', r.text, re.DOTALL)
        if not match:
            raise RuntimeError("Failed to extract __NEXT_DATA__")
        data = json.loads(match.group(1))
        return data["buildId"]

    def get_titles(self) -> Titles_T:
        build_id = self._get_build_id(self.slug)

        if self.kind == "serie":
            url = self.config["endpoints"]["metadata_series"].format(build_id=build_id, slug=self.slug)
        else:
            url = self.config["endpoints"]["metadata"].format(build_id=build_id, slug=self.slug)

        resp = self.session.get(url)
        resp.raise_for_status()
        queries = resp.json()["pageProps"]["dehydratedState"]["queries"]

        def get_data(fragment: str):
            return next((q["state"]["data"] for q in queries if fragment in str(q.get("queryKey", ""))), None)

        if self.kind == "serie":
            series_data = get_data("series:detail-")
            if not series_data:
                raise ValueError("Series metadata not found")

            episodes = []
            seasons = get_data("series:seasons-") or []
            for season in seasons:
                eps = get_data(f"programs:season-{season['guid']}") or []
                for e in eps:
                    episodes.append(
                        Episode(
                            id_=e["guid"],
                            service=self.__class__,
                            title=series_data["title"],
                            season=int(season["seasonKey"]),
                            number=int(e["programKey"]),
                            name=e["title"],
                            description=(e.get("synopsis", {}) or {}).get("long", ""),
                            language=Language.get("nl"),
                            data=e,
                        )
                    )
            return Series(episodes)

        # Movie
        item = get_data("program:detail-") or queries[0]["state"]["data"]
        synopsis = item.get("synopsis", {})
        desc = synopsis.get("long") or synopsis.get("short", "") if isinstance(synopsis, dict) else str(synopsis)
        year = (int(item["firstBroadcastDate"]) // 31536000 + 1970) if item.get("firstBroadcastDate") else None

        return Movies([
            Movie(
                id_=item["guid"],
                service=self.__class__,
                name=item["title"],
                description=desc,
                year=year,
                language=Language.get("nl"),
                data=item,
            )
        ])

    def get_tracks(self, title: Title_T) -> Tracks:
        product_id = title.data.get("productId")
        if not product_id:
            raise ValueError("no productId detected.")

        # Get JWT
        token_url = self.config["endpoints"]["player_token"].format(product_id=product_id)
        r_tok = self.session.get(token_url, headers={"Referer": f"https://npo.nl/start/video/{self.slug}"})
        r_tok.raise_for_status()
        jwt = r_tok.json()["jwt"]

        # Request stream
        r_stream = self.session.post(
            self.config["endpoints"]["streams"],
            json={
                "profileName": "dash",
                "drmType": "widevine",
                "referrerUrl": f"https://npo.nl/start/video/{self.slug}",
                "ster": {"identifier": "npo-app-desktop", "deviceType": 4, "player": "web"},
            },
            headers={
                "Authorization": jwt,
                "Content-Type": "application/json",
                "Origin": "https://npo.nl",
                "Referer": f"https://npo.nl/start/video/{self.slug}",
            },
        )
        r_stream.raise_for_status()
        data = r_stream.json()

        if "error" in data:
            raise PermissionError(f"Stream error: {data['error']}")

        stream = data["stream"]
        manifest_url = stream.get("streamURL") or stream.get("url")
        if not manifest_url:
            raise ValueError("No stream URL in response")

        is_unencrypted = "unencrypted" in manifest_url.lower() or not any(k in stream for k in ["drmToken", "token"])

        # Parse DASH
        tracks = DASH.from_url(manifest_url, session=self.session).to_tracks(language=title.language)

        # Subtitles
        subtitles = []
        for sub in data.get("assets", {}).get("subtitles", []):
            lang = sub.get("iso", "und")
            subtitles.append(
                Subtitle(
                    id_=sub.get("name", lang),
                    url=sub["location"].strip(),
                    language=Language.get(lang),
                    is_original_lang=lang == "nl",
                    codec=Subtitle.Codec.WebVTT,
                    name=sub.get("name", "Unknown"),
                    forced=False,
                    sdh=False,
                )
            )
        tracks.subtitles = subtitles

        # DRM
        if is_unencrypted:
            for tr in tracks.videos + tracks.audio:
                if hasattr(tr, "drm") and tr.drm:
                    tr.drm.clear()
        else:
            self.drm_token = stream.get("drmToken") or stream.get("token") or stream.get("drm_token")
            if not self.drm_token:
                raise ValueError(f"No DRM token found. Available keys: {list(stream.keys())}")

            for tr in tracks.videos + tracks.audio:
                if getattr(tr, "drm", None):
                    tr.drm.license = lambda challenge, **kw: self.get_widevine_license(
                        challenge=challenge, title=title, track=tr
                    )

        return tracks

    def get_chapters(self, title: Title_T) -> list[Chapter]:
        return []

    def get_widevine_license(self, challenge: bytes, title: Title_T, track: AnyTrack) -> bytes:
        if not self.drm_token:
            raise ValueError("DRM token not set – login or paid content may be required.")
        r = self.session.post(
            self.config["endpoints"]["widevine_license"],
            params={"custom_data": self.drm_token},
            data=challenge,
        )
        r.raise_for_status()
        return r.content