from __future__ import annotations

import base64
import click
import re
import secrets
import sys
import uuid

from click import Context
from collections.abc import Generator
from datetime import datetime
from http.cookiejar import CookieJar
from langcodes import Language
from pyplayready.cdm import Cdm as PlayReadyCdm
from requests import Request
from typing import Any, Optional, Union, List

from unshackle.core.constants import AnyTrack
from unshackle.core.credential import Credential
from unshackle.core.manifests import HLS
from unshackle.core.search_result import SearchResult
from unshackle.core.service import Service
from unshackle.core.titles import Title_T, Titles_T, Episode, Movie, Movies, Series
from unshackle.core.tracks import Chapter, Chapters, Tracks, Attachment, Video, Audio, Subtitle
from unshackle.core.utilities import get_ip_info
from unshackle.core.utils.collections import as_list

from . import queries

class DSNP(Service):
    """
    Service code for Disney+ Streaming Service (https://disneyplus.com).

    Author: Made by CodeName393 with Special Thanks to narakama\n
    Authorization: Credentials\n
    Security: UHD@L1/SL3000 FHD@L1/SL3000 HD@L3/SL2000
    """

    ALIASES = ("DSNP", "disneyplus", "disney+")
    TITLE_RE = (
        r"^(?:https?://(?:www\.)?disneyplus\.com(?:/[a-z0-9-]+)?(?:/[a-z0-9-]+)?/(browse)/(?P<id>entity-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}))(?:\?.*)?$",
        r"^(?:https?://(?:www\.)?disneyplus\.com(?:/[a-z0-9-]+)?(?:/[a-z0-9-]+)?/(movies|series)/[a-z0-9-]+/)?(?P<id>[a-zA-Z0-9-]+)(?:\?.*)?$",
    )

    @staticmethod
    @click.command(name="DisneyPlus", short_help="https://disneyplus.com", help=__doc__)
    @click.argument("title", type=str)
    @click.option("--imax", is_flag=True, default=False, help="Prefer IMAX Enhanced version if available.")
    @click.option("--remastered-ar", is_flag=True, default=False, help="Prefer Remastered Aspect Ratio if available.")
    @click.option("-ext", "--extras", is_flag=True, default=False, help="Select a extras video if available.")
    @click.pass_context
    def cli(ctx: Context, **kwargs: Any) -> DSNP:
        return DSNP(ctx, **kwargs)

    def __init__(self, ctx: Context, title: str, imax: bool, remastered_ar: bool, extras: bool):
        self.title = title
        super().__init__(ctx)
        
        self.title_id = self.title
        for pattern in self.TITLE_RE:
            match = re.match(pattern, self.title)
            if match:
                self.title_id = match.group("id")
                break

        self.prefer_imax = imax
        self.prefer_remastered_ar = remastered_ar
        self.extras = extras

        self.vcodec = ctx.parent.params.get("vcodec") or Video.Codec.AVC
        self.acodec : Audio.Codec = ctx.parent.params.get("acodec")
        self.range = ctx.parent.params.get("range_") or [Video.Range.SDR]
        self.quality: List[int] = ctx.parent.params.get("quality") or [1080]
        self.wanted = ctx.parent.params.get("wanted")
        self.audio_only = ctx.parent.params.get("audio_only")
        self.subs_only = ctx.parent.params.get("subs_only")
        self.chapters_only = ctx.parent.params.get("chapters_only")

        self.cdm = ctx.obj.cdm
        self.playready = isinstance(self.cdm, PlayReadyCdm) if self.cdm else False
        self.is_l3 = (self.cdm.security_level < 3000) if self.playready else (self.cdm.security_level == 3) if self.cdm else False

        self.region = None
        self.prod_config = {}
        self.account_tokens = {}
        self.active_session = {}
        self.playback_data = {}

        self.log.info("Preparing...")

        if self.is_l3:
            self.vcodec = Video.Codec.AVC
            self.range = [Video.Range.SDR]
            self.quality = [720]
            self.log.warning(" + Switched video to HD. This CDM only support HD.")
        else:
            if self.quality > [1080] and self.range[0] == [Video.Range.SDR]:
                self.range = [Video.Range.HDR10]
                self.log.info(" + Switched range to HDR10. 4K resolution requires HDR.")

            if (self.range != [Video.Range.SDR] or self.quality > [1080]) and self.vcodec != Video.Codec.HEVC:
                self.vcodec = Video.Codec.HEVC
                self.log.info(f" + Switched video codec to H265 to be able to get {self.range[0]} dynamic range.")

            if self.acodec == Audio.Codec.DTS and not self.prefer_imax:
                self.prefer_imax = True
                self.log.info(" + Switched IMAX prefer. DTS audio can only be get from IMAX prefer.")

        self.session.headers.update({
            "User-Agent": self.config["bamsdk"]["user_agent"],
            "Accept-Encoding": "gzip",
            "Accept": "application/json",
            "Content-Type": "application/json"
        })

        ip_info = get_ip_info(self.session)
        country_key = None
        possible_keys = ["countryCode", "country", "country_code", "country-code"]
        for key in possible_keys:
            if key in ip_info:
                country_key = key
                break
        if country_key:
            self.region = str(ip_info[country_key]).upper()
            self.log.info(f" + IP Region: {self.region}")
        else:
            self.log.warning(f" - The region could not be determined from IP information: {ip_info}")
            self.region = "US"
            self.log.info(f" + IP Region: {self.region} (By Default)")

        self.prod_config = self.session.get(self.config["endpoints"]["config"]).json()

        self.session.headers.update({
            "X-Application-Version": self.config["bamsdk"]["application_version"],
            "X-BAMSDK-Client-ID": self.config["bamsdk"]["client"],
            "X-BAMSDK-Platform": self.config["device"]["platform"],
            "X-BAMSDK-Version": self.config["bamsdk"]["sdk_version"],
            "X-DSS-Edge-Accept": "vnd.dss.edge+json; version=2",
            "X-Request-Yp-Id": self.config["bamsdk"]["yp_service_id"]
        })

    def authenticate(self, cookies: Optional[CookieJar] = None, credential: Optional[Credential] = None) -> None:
        super().authenticate(cookies, credential)
        self.credentials = credential
        if not credential:
            raise EnvironmentError("Service requires Credentials for Authentication.")

        self.log.info("Logging into Disney+...")
        self._login()

        if self.config.get("profile") and "index" in self.config["profile"]:
            try:
                target_profile_index = int(self.config["profile"]["index"])
            except (ValueError, TypeError, KeyError):
                self.log.error(" - Profile index in configuration is invalid.", exc_info=False)
                sys.exit(1)

            profiles = self.active_session['account']['profiles']
            if not 0 <= target_profile_index < len(profiles):
                self.log.error(f" - Invalid profile index: {target_profile_index}. Please choose between 0 and {len(profiles) - 1}.", exc_info=False)
                sys.exit(1)
            
            target_profile = profiles[target_profile_index]
            active_profile_id = self.active_session['account']['activeProfile']['id']

            if target_profile['id'] != active_profile_id:
                self._perform_switch_profile(target_profile, self.session.headers)

                self.log.info(" + Refreshing session data after profile switch...")
                full_account_info = self._get_account_info()
                self.active_session = full_account_info["activeSession"]
                self.active_session['account'] = full_account_info['account']
                self.log.info("Session data updated successfully.")

        self.log.debug(self.active_session)

        if not self.active_session['isSubscriber']:
            self.log.error(" - Cannot continue, account is not subscribed to Disney+", exc_info=False)
            sys.exit(1)
        if not self.active_session['inSupportedLocation']:
            self.log.error(" - Cannot continue, Not available in your Region.", exc_info=False)
            sys.exit(1)

        self.log.info(f" + Account ID: {self.active_session['account']['id']}")
        self.log.info(f" + Profile ID: {self.active_session['account']['activeProfile']['id']}")
        self.log.info(f" + Subscribed: {self.active_session['isSubscriber']}")
        self.log.debug(f" + Account Region: {self.active_session['homeLocation']['countryCode']}")
        self.log.debug(f" + Detected Location: {self.active_session['location']['countryCode']}")
        self.log.debug(f" + Supported Location: {self.active_session['inSupportedLocation']}")

        active_profile_id = self.active_session['account']['activeProfile']['id']
        full_profile_object = next(
            p for p in self.active_session['account']['profiles'] if p['id'] == active_profile_id
        )

        current_imax_setting = full_profile_object["attributes"]["playbackSettings"]["preferImaxEnhancedVersion"]
        self.log.info(f" + IMAX Enhanced: {current_imax_setting}")
        if current_imax_setting is not self.prefer_imax:
            update_tokens = self._set_imax_preference(self.prefer_imax)
            self._apply_new_tokens(update_tokens["token"])

        current_133_setting = full_profile_object["attributes"]["playbackSettings"]["prefer133"] # Original Aspect Ratio
        self.log.info(f" + Remastered Aspect Ratio: {not current_133_setting}")
        if not current_133_setting is not self.prefer_remastered_ar:
            update_tokens = self._set_remastered_ar_preference(self.prefer_remastered_ar)
            self._apply_new_tokens(update_tokens["token"])

    def _login(self) -> None:
        cache = self.cache.get(f"tokens_{self.region}_{self.credentials.sha1}")

        if cache:
            try:
                self.log.info(" + Using cached tokens...")
                self.account_tokens = cache.data

                bearer = self.account_tokens["accessToken"]
                if not bearer:
                    raise ValueError("accessToken not found in cache")
                self.session.headers.update({'Authorization': f'Bearer {bearer}'})

            except (KeyError, ValueError, TypeError) as e:
                self.log.warning(f" - Cached token data is invalid or corrupted ({e}). Getting new tokens...")
                self._perform_full_login()

            try:
                self._refresh()
            except Exception as e:
                self.log.warning(f" - Failed to refresh token from cache ({e}). Getting new tokens...")
                self._perform_full_login()

            # No problem if don't use it
            # self._update_device()

        else:
            self.log.info(" + Getting new tokens...")
            self._perform_full_login()

        self.log.info(" + Fetching session data...")
        full_account_info = self._get_account_info()
        self.active_session = full_account_info["activeSession"]
        self.active_session['account'] = full_account_info['account']
        self.log.info("Session data setup successfully.")

    def _perform_full_login(self) -> None:
        android_id = secrets.token_bytes(8).hex()
        drm_id = f"{base64.urlsafe_b64encode(secrets.token_bytes(32)).decode('utf-8')}\n"
        device_token = self._register_device(android_id, drm_id)

        email_status = self._check_email(self.credentials.username, device_token)

        if email_status.lower() != "login":
            if email_status.lower() == "otp":
                self.log.warning(" - Account requires OTP code login.")
                self._request_otp(self.credentials.username, device_token)

                otp_code = None
                try:
                    otp_code = input("Enter a OTP code (Check email): ")
                    if not otp_code:
                        self.log.error("   - OTP code is required, but no value was entered.", exc_info=False)
                        sys.exit(1)
                    if not otp_code.isdigit():
                        self.log.error("   - Invalid OTP code. Please enter only numbers.", exc_info=False)
                        sys.exit(1)
                    if len(otp_code) < 6:
                        self.log.error("   - OTP code is too short. Please enter at least 6 digits.", exc_info=False)
                        sys.exit(1)
                    if len(otp_code) > 6:
                        self.log.warning("   - OTP code is longer than 6 digits. Using the first 6 digits.")
                        otp_code = otp_code[:6]
                except KeyboardInterrupt:
                    self.log.error("\n - OTP code input cancelled by user.", exc_info=False)
                    sys.exit(1)
                
                auth_action = self._auth_action_with_otp(self.credentials.username, otp_code, device_token)
                login_tokens = self._login_with_auth_action(auth_action, device_token)

            elif email_status.lower() == "register":
                self.log.error(" - Account is not registered. Please register first.", exc_info=False)
                sys.exit(1)
            else:
                self.log.error(f" - Email status is '{email_status}'. Account status verification required.", exc_info=False)
                sys.exit(1)

        else:
            login_tokens = self._login_with_password(self.credentials.username, self.credentials.password, device_token)

        temp_auth_header = {"Authorization": f'Bearer {login_tokens["token"]["accessToken"]}'}
        account_info = self._get_account_info(temp_auth_header)
        profiles = account_info["account"]["profiles"]

        selected_profile = None
        if self.config.get("profile") and "index" in self.config["profile"]:
            try:
                profile_index = int(self.config["profile"]["index"])
                if not 0 <= profile_index < len(profiles):
                    raise ValueError(f"Index out of range (0-{len(profiles)-1})")
                
                selected_profile = profiles[profile_index]
            except (ValueError, TypeError):
                self.log.error(" - Profile index in configuration is invalid.", exc_info=False)
                sys.exit(1)
        else:
            selected_profile = next(
                (p for p in profiles if not p["attributes"]["kidsModeEnabled"] and not p["attributes"]["parentalControls"]["isPinProtected"]),
                None
            )
            if not selected_profile:
                self.log.error(" - Auto-selection failed: No suitable profile found (non-kids, no PIN). Please configure a specific profile.", exc_info=False)
                sys.exit(1)

        if selected_profile:
            self._perform_switch_profile(selected_profile, temp_auth_header)

    def _perform_switch_profile(self, target_profile: dict, auth_headers: dict) -> None:
        self.log.info(f" + Switching to profile: {target_profile['name']}({target_profile['id']})")

        if target_profile['attributes']['kidsModeEnabled']:
            self.log.error("   - Kids Profile and cannot be used.", exc_info=False)
            sys.exit(1)

        profile_pin = None
        if target_profile['attributes']['parentalControls']['isPinProtected']:
            self.log.warning("   - This profile is PIN protected.")
            try:
                profile_pin = input("Enter a profile pin: ")
                if not profile_pin:
                    self.log.error("   - PIN is required, but no value was entered.", exc_info=False)
                    sys.exit(1)
                if not profile_pin.isdigit():
                    self.log.error("   - Invalid PIN. Please enter only numbers.", exc_info=False)
                    sys.exit(1)
                if len(profile_pin) < 4:
                    self.log.error("   - PIN is too short. Please enter at least 4 digits.", exc_info=False)
                    sys.exit(1)
                if len(profile_pin) > 4:
                    self.log.warning("   - PIN is longer than 4 digits. Using the first 4 digits.")
                    profile_pin = profile_pin[:4]
            except KeyboardInterrupt:
                self.log.error("\n - PIN input cancelled by user.", exc_info=False)
                sys.exit(1)

        switch_profile_data = self._switch_profile(target_profile['id'], auth_headers, profile_pin)
        self._apply_new_tokens(switch_profile_data["token"])
        
    def _refresh(self) -> str:
        cache = self.cache.get(f"tokens_{self.region}_{self.credentials.sha1}")
        if not cache.expired:
            self.log.debug(f" + Token is valid until: {datetime.fromtimestamp(cache.expiration.timestamp()).strftime('%Y-%m-%d %H:%M:%S')}")
            return self.session.headers.get('Authorization', 'Bearer ').split(' ')[1]

        self.log.warning(" + Token expired. Refreshing...")
        try:
            refreshed_data = self._refresh_token(self.account_tokens["refreshToken"])
            self._apply_new_tokens(refreshed_data["token"])
        except Exception as _:
            raise Exception("Refresh Token Expired")
        
    def _apply_new_tokens(self, token_data: dict) -> str:
        self.account_tokens = token_data

        bearer = self.account_tokens["accessToken"]
        if not bearer:
            self.log.error("Invalid token data: accessToken not found.", exc_info=False)
            sys.exit(1)
        self.session.headers.update({'Authorization': f'Bearer {bearer}'})

        expires_in = self.account_tokens["expiresIn"] or 3600
        cache = self.cache.get(f"tokens_{self.region}_{self.credentials.sha1}")
        cache.set(self.account_tokens, expires_in - 60)
        self.log.debug(f"  + New Token is valid until: {datetime.fromtimestamp(cache.expiration.timestamp()).strftime('%Y-%m-%d %H:%M:%S')}")

        return bearer
    
    def search(self) -> Generator[SearchResult, None, None]:
        params = {"query": self.title}
        endpoint = self._href(self.prod_config["services"]["explore"]["client"]["endpoints"]["search"]["href"])
        data = self._request("GET", endpoint, params=params)["data"]["page"]
        if not data.get("containers"):
            return

        results = data["containers"][0]["items"]
        for result in results:
            entity = "entity-" + result["id"]
            yield SearchResult(
                id_=entity,
                title=result["visuals"]["title"],
                description=result["visuals"]["description"]["brief"],
                label=result["visuals"]["metastringParts"]["releaseYearRange"]["startYear"],
                url=f"https://www.disneyplus.com/browse/{entity}",
            )

    def get_titles(self) -> Titles_T:
        if not self.extras:
            try:
                content_info = self._get_deeplink(self.title_id)
                content_type = content_info["data"]["deeplink"]["actions"][0]["contentType"]
            except Exception as e:
                try:
                    actions_info = self._get_deeplink_last(self.title_id)
                    if actions_info["data"]["deeplink"]["actions"][0]["type"] == "browse":
                        info_block = base64.b64decode(actions_info["data"]["deeplink"]["actions"][0]["infoBlock"])
                        if b"movie" in info_block:
                            content_type = "movie"
                        elif b"series" in info_block:
                            content_type = "series"
                        else:
                            content_type = "other"
                            self.log.warning(" - The content is not standard. however, it tries to look up the data.")
                except Exception as e:
                    self.log.error(f" - Failed to determine content type via deeplink ({e}).", exc_info=False)
                    sys.exit(1)
        else:
            content_type = "extras"
        self.log.debug(f" + Content Type: {content_type.upper()}")

        page = self._get_page(self.title_id)

        year = None
        if year_data := page["visuals"]["metastringParts"].get("releaseYearRange"):
            year = year_data.get("startYear")

        if content_type != "extras":
            playback_action = next(
                (x for x in page["actions"] if x["type"] == "playback"),
                None
            )
            if not playback_action:
                self.log.error(f" - No content is available. (Playback action not found)", exc_info=False)
                sys.exit(1)
            lang_data = self._get_original_lang(playback_action["availId"])
            player_exp = lang_data["data"]["playerExperience"]
            orig_lang = player_exp.get("originalLanguage") or player_exp.get("targetLanguage") or "en"
            self.log.debug(f' + Original Language: {orig_lang}')
        
        if content_type in ("movie", "other"):
            return Movies([
                Movie(
                    id_=page["id"],
                    service=self.__class__,
                    name=page["visuals"]["title"],
                    year=year,
                    language=Language.get(orig_lang),
                    data=page
                )
            ])

        elif content_type == "series":
            return Series(self._get_series(page, year, orig_lang))
        
        elif content_type == "extras":
            return Series(self._get_extras(page, year))

        else:
            self.log.error(f" - Unsupported content type: {content_type}", exc_info=False)
            sys.exit(1)

    def _get_series(self, page: dict, year: int, orig_lang: str) -> Series:
        container = next(x for x in page["containers"] if x["type"] == "episodes")
        season_ids = [s["id"] for s in container["seasons"]]

        episodes : List[Episode] = []
        for season_id in season_ids:
            episodes_data =  self._get_episodes_data(season_id)

            for ep in episodes_data:
                if ep["type"] != "view":
                    continue

                episodes.append(
                    Episode(
                        id_=ep["id"],
                        service=self.__class__,
                        title=page["visuals"]["title"],
                        season=int(ep["visuals"]["seasonNumber"]),
                        number=int(ep["visuals"]["episodeNumber"]),
                        name=ep["visuals"]["episodeTitle"],
                        year=year,
                        language=Language.get(orig_lang),
                        data=ep
                    )
                )

        return episodes
    
    def _get_extras(self, page: dict, year: int) -> Series:
        extras_containers = [
            x for x in page["containers"]
            if x["type"] == "set" and x["style"]["name"] == "standard_compact_list"
        ]

        if not extras_containers:
            self.log.error(" - No extras found.", exc_info=False)
            sys.exit(1)

        extras_episodes : List[Episode] = []
        ep_count = 1

        first_item = extras_containers[0]["items"][0]
        first_action = next((x for x in first_item["actions"] if x["type"] in ("playback", "trailer")), None)
        if first_action:
            lang_data = self._get_original_lang(first_action["availId"])
            player_exp = lang_data["data"]["playerExperience"]
            orig_lang = player_exp.get("originalLanguage") or player_exp.get("targetLanguage") or "en"
            self.log.debug(f' + Original Language: {orig_lang}')

        for container in extras_containers:
            items = container["items"]
            for item in items:
                if item["type"] == "view":
                    action = next((x for x in item["actions"] if x["type"] in ("playback", "trailer")), None)
                    
                    if action:
                        extras_episodes.append(
                            Episode(
                                id_=item["id"],
                                service=self.__class__,
                                title=page["visuals"]["title"],
                                season=0, # Special
                                number=ep_count,
                                name=item["visuals"]["title"],
                                year=year,
                                language=Language.get(orig_lang),
                                data=item
                            )
                        )
                        ep_count += 1

        if not extras_episodes:
            self.log.error(" - No playable extras found.", exc_info=False)
            sys.exit(1)

        return extras_episodes

    def get_tracks(self, title: Title_T) -> Tracks:
        playback = next(x for x in title.data["actions"] if x.get("type") == "playback")
        media_id = playback["resourceId"] or None
        if not media_id:
            self.log.error(" - Failed to get media ID for playback info", exc_info=False)
            sys.exit(1)

        scenario = "ctr-regular" if self.is_l3 else "ctr-high" # cbcs-high

        self.log.debug(f"Playback Scenario: {scenario}")
        self.log.debug(f"Media ID: {media_id}")

        self._refresh() # Safe Access

        if Video.Range.HYBRID in self.range[0] and not self.is_l3:
            self.log.warning("DV+HDR Multi-range requested.")

            self.log.info(" + Fetching Dolby Vision tracks...")
            tracks = self._fetch_manifest_tracks(title, media_id, scenario, ["DOLBY_VISION"])

            self.log.info(" + Fetching HDR10 tracks...")
            hdr_tracks_temp = self._fetch_manifest_tracks(title, media_id, scenario, ["HDR10"]) # HDR10PLUS

            tracks.add(hdr_tracks_temp, warn_only=True)
        else:
            video_ranges = []
            if not self.is_l3:
                if Video.Range.DV in self.range[0]:
                    video_ranges = ["DOLBY_VISION"]
                elif Video.Range.HDR10 in self.range[0] or Video.Range.HDR10P in self.range[0]:
                    video_ranges = ["HDR10"] # HDR10PLUS

            tracks = self._fetch_manifest_tracks(title, media_id, scenario, video_ranges or None)

        tracks.add(self._get_thumbnail(title))
        return self._post_process_tracks(tracks)

    def _fetch_manifest_tracks(self, title: Title_T, media_id: str, scenario: str, video_ranges: List[str] = None) -> Tracks:
        attributes = {
            "codecs": {
                "supportsMultiCodecMaster": False,
                "video": ["h.264"]
            },
            "protocol": "HTTPS",
            "frameRates": [60],
            "assetInsertionStrategies": {
                "point": "SGAI", # Server-Guided Ad Insertion
                "range": "SGAI" # Server-Guided Ad Insertion
            },
            "playbackInitiationContext": "ONLINE",
            "slugDuration": "SLUG_500_MS", # SLUG_1000_MS, SLUG_750_MS ?
            "maxSlideDuration": "4_HOUR" # 15_MIN ?
        }

        if self.is_l3:
            attributes["resolution"] = {"max": ["1280x720"]}
        else:
            attributes["resolution"] = {"max": ["3840x2160"]}

            if self.vcodec == Video.Codec.HEVC:
                attributes["codecs"]["video"] = ["h.264", "h.265"]

            attributes["audioTypes"] = ["ATMOS", "DTS_X"]
            
            if video_ranges:
                attributes["videoRanges"] = video_ranges

        payload = {
            "playbackId": media_id,
            "playback": {
                "attributes": attributes
            }
        }
        self.playback_data[title.id] = self._get_playback(scenario, payload)
        manifest_url = self.playback_data[title.id]["sources"][0]['complete']['url']
        self.log.debug(f" + Manifest URL: {manifest_url}")
        return HLS.from_url(url=manifest_url, session=self.session).to_tracks(title.language)
    
    def _get_thumbnail(self, title: Title_T) -> Attachment:
        if type(title) == Movie:
            thumbnail_id = title.data["visuals"]["artwork"]["standard"]["background"]["1.78"]["imageId"]
        elif type(title) == Episode:
            thumbnail_id = title.data["visuals"]["artwork"]["standard"]["thumbnail"]["1.78"]["imageId"]
        thumbnail_url = self._href(
            self.prod_config["services"]["ripcut"]["client"]["endpoints"]["mainCompose"]["href"],
            version="v2",
            partnerId="disney",
            imageId=thumbnail_id
        )
        return Attachment.from_url(url=thumbnail_url, name=thumbnail_id, mime_type="image/png")

    def _post_process_tracks(self, tracks: Tracks) -> Tracks:
        for track in tracks:
            if isinstance(track, (Audio, Subtitle)):
                track.name = "[Original]" if track.is_original_lang else None

        for audio in tracks.audio:
            bitrate_match = re.search(r"(?<=composite_)\d+|\d+(?=_(?:hdri|complete))|(?<=-)\d+(?=K/)", as_list(audio.url)[0])
            if bitrate_match:
                audio.bitrate = int(bitrate_match.group()) * 1000
                if audio.bitrate == 1_000_000:
                    audio.bitrate = 768_000 # DSNP lies about the Atmos bitrate
            if audio.channels == 6.0:
                audio.channels = 5.1
            if audio.channels == 10.0: # DTS-UHD
                audio.channels = "5.1.4" # Unshackle does not recommend
                audio.codec = Audio.Codec.DTS
                audio.drm = None

        for subtitle in tracks.subtitles:
            subtitle.codec = Subtitle.Codec.WebVTT

        return tracks
    
    def get_chapters(self, title: Title_T) -> Chapters:
        try:
            editorial = self.playback_data[title.id]["editorial"]
            if not editorial:
                return Chapters()
            
            LABEL_MAP = {
                "intro_start": "intro_start",
                "intro_end": "intro_end",
                "recap_start": "recap_start",
                "recap_end": "recap_end",
                "FFER": "recap_start", # First Frame Episode Recap
                "LFER": "recap_end", # Last Frame Episode Recap 
                "FFEI": "intro_start", # First Frame Episode Intro
                "LFEI": "intro_end", # Last Frame Episode Intro
                "FFEC": "credits_start", # First Frame End Credits
                "LFEC": "lfec_marker", # Last Frame End Credits
                "up_next": None,
                "tag_start": None,
                "tag_end": None,
            }
            
            NAME_MAP = {
                "recap_start": "Recap",
                "recap_end": "Scene",
                "intro_start": "Intro",
                "intro_end": "Scene",
                "credits_start": "Credits",
            }

            grouped = {}
            for marker in editorial:
                group = LABEL_MAP.get(marker["label"])
                offset = marker["offsetMillis"]
                if group and offset is not None:
                    grouped.setdefault(group, []).append(offset)

            raw_chapters = []
            total_runtime = title.data["visuals"]["metastringParts"]["runtime"]["runtimeMs"]

            for group, times in grouped.items():
                if not times: continue

                timestamp = min(times) if "start" in group else max(times) if "end" in group else times[0]
                name = NAME_MAP.get(group)
                
                if group == "lfec_marker" and (total_runtime - timestamp) > 5000:
                    name = "Scene"

                if name:
                    raw_chapters.append((timestamp, name))

            raw_chapters.sort(key=lambda x: x[0])
            unique_chapters = []
            seen_ms = set()
            
            for ms, name in raw_chapters:
                if ms not in seen_ms:
                    unique_chapters.append({"ms": ms, "name": name})
                    seen_ms.add(ms)

            if not unique_chapters:
                unique_chapters.append({"ms": 0, "name": "Scene"})
            else:
                first = unique_chapters[0]
                if first["ms"] > 0:
                    if first["ms"] < 5000 and first["name"] in ("Intro", "Recap"):
                        first["ms"] = 0
                    else:
                        unique_chapters.insert(0, {"ms": 0, "name": "Scene"})

            chapters: List[Chapter] = []
            for i, chap_data in enumerate(unique_chapters):
                time_sec = chap_data["ms"] / 1000.000
                chapter_title = chap_data["name"]
                chapters.append(
                    Chapter(
                        timestamp=float(time_sec),
                        name=chapter_title if chapter_title != "Scene" else None
                    )
                )
            
            return chapters

        except Exception as e:
            self.log.warning(f"Failed to extract chapters: {e}")
            return Chapters()

    def get_widevine_service_certificate(self, *, challenge: bytes, title: Title_T, track: AnyTrack) -> Union[bytes, str]:
        # endpoint = self.prod_config["services"]["drm"]["client"]["endpoints"]["widevineCertificate"]["href"]
        # res = self.session.get(endpoint, data=challenge)
        return self.config["certificate"]

    def get_widevine_license(self, *, challenge: bytes, title: Title_T, track: AnyTrack) -> Optional[Union[bytes, str]]:
        self._refresh() # Safe Access
        endpoint = self.prod_config["services"]["drm"]["client"]["endpoints"]["widevineLicense"]["href"]
        headers = {"Content-Type": "application/octet-stream"}

        try:
            res = self.session.post(endpoint, headers=headers, data=challenge)
            res.raise_for_status()
        except Exception as e:
            self.log.error(f"License request failed: {e}", exc_info=False)
            sys.exit(1)
        return res.content

    def get_playready_license(self, *, challenge: bytes, title: Title_T, track: AnyTrack) -> Optional[bytes]:
        self._refresh() # Safe Access
        endpoint = self.prod_config["services"]["drm"]["client"]["endpoints"]["playReadyLicense"]["href"]
        headers = {
            "Accept": "application/xml, application/vnd.media-service+json; version=2",
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": "http://schemas.microsoft.com/DRM/2007/03/protocols/AcquireLicense"
        }
        try:
            res = self.session.post(endpoint, headers=headers, data=challenge)
            res.raise_for_status()
        except Exception as e:
            self.log.error(f"License request failed: {e}", exc_info=False)
            sys.exit(1)
        return res.content
    
    def _get_deeplink(self, ref_id: str) -> dict:
        endpoint = self._href(
            self.prod_config["services"]["content"]["client"]["endpoints"]["getDeeplink"]["href"],
            refIdType="deeplinkId",
            refId=ref_id
        )
        data =  self._request("GET", endpoint)
        return data
    
    def _get_deeplink_last(self, ref_id: str) -> dict:
        endpoint = self._href(self.prod_config["services"]["explore"]["client"]["endpoints"]["getDeeplink"]["href"])
        params = {
            "refIdType" : "deeplinkId",
            "refId" : ref_id
        }
        data =  self._request("GET", endpoint, params=params)
        return data

    def _get_page(self, title_id: str) -> dict:
        endpoint = self._href(
            self.prod_config["services"]["explore"]["client"]["endpoints"]["getPage"]["href"],
            pageId=title_id
        )
        data = self._request("GET", endpoint, params={"disableSmartFocus": "true", "limit": 999})
        return data["data"]["page"]

    def _get_original_lang(self, availId: str) -> dict:
        endpoint = self._href(
            self.prod_config["services"]["explore"]["client"]["endpoints"]["getPlayerExperience"]["href"],
            availId=availId
        )
        data =  self._request("GET", endpoint)
        return data
    
    def _get_episodes_data(self, season_id: str) -> List[dict]:
        endpoint = self._href(
            self.prod_config["services"]["explore"]["client"]["endpoints"]["getSeason"]["href"],
            seasonId=season_id
        )
        data = self._request("GET", endpoint, params={'limit': 999})["data"]["season"]["items"]
        return data

    def _get_playback(self, scenario: str, payload: dict) -> dict:
        endpoint = self._href(
            self.prod_config["services"]["media"]["client"]["endpoints"]["mediaPayload"]["href"],
            scenario=scenario
        )
        headers = {
            "Accept": "application/vnd.media-service+json",
            "X-DSS-Feature-Filtering": "true"
        }
        data = self._request("POST", endpoint, headers=headers, payload=payload)
        return data["stream"]

    def _register_device(self, android_id: str, drm_id: str) -> str:
        endpoint = self.prod_config["services"]["orchestration"]["client"]["endpoints"]["registerDevice"]["href"]
        headers = {
            "Authorization": self.config["bamsdk"]["api_key"],
            "X-BAMSDK-Platform-Id": self.config["device"]["platform_id"]
        }
        payload = {
            "variables": {
                "registerDevice": {
                    "applicationRuntime": self.config["device"]["applicationRuntime"], 
                    "attributes": {
                        "osDeviceIds": [
                            {
                                "identifier": android_id,
                                "type": "android.vendor.id"
                            },
                            {
                                "identifier": drm_id,
                                "type": "android.drm.id"
                            }
                        ],
                        "operatingSystem": self.config["device"]["operatingSystem"],
                        "operatingSystemVersion": self.config["device"]["operatingSystemVersion"]
                    },
                    "deviceFamily": self.config["device"]["family"], 
                    "deviceLanguage": self.config["device"]["deviceLanguage"], 
                    "deviceProfile": self.config["device"]["profile"],
                    "devicePlatformId": self.config["device"]["platform_id"],
                }
            },
            "query": queries.REGISTER_DEVICE
        }
        data = self._request("POST", endpoint, payload=payload, headers=headers)
        return data["extensions"]["sdk"]["token"]["accessToken"]

    def _check_email(self, email: str, token: str) -> str:
        endpoint = self.prod_config["services"]["orchestration"]["client"]["endpoints"]["query"]["href"]
        headers = {
            "Authorization": token,
            "X-BAMSDK-Platform-Id": self.config["device"]["platform_id"]
        }
        payload = {
            "operationName": "check",
            "variables": {
                "email": email
            },
            "query": queries.CHECK_EMAIL
        }
        data = self._request("POST", endpoint, payload=payload, headers=headers)
        return data["data"]["check"]["operations"][0]
    
    def _login_with_password(self, email: str, password: str, token: str) -> str:
        endpoint = self.prod_config["services"]["orchestration"]["client"]["endpoints"]["query"]["href"]
        headers = {
            "Authorization": token,
            "X-BAMSDK-Platform-Id": self.config["device"]["platform_id"]
        }
        payload = {
            "operationName": "login",
            "variables": {
                "input": {
                    "email": email,
                    "password": password
                },
                "includeIdentity": True,
                "includeAccountConsentToken": True
            },
            "query": queries.LOGIN
        }
        data = self._request("POST", endpoint, payload=payload, headers=headers)
        return data["extensions"]["sdk"]
    
    def _request_otp(self, email: str, token: str) -> dict:
        endpoint = self.prod_config["services"]["orchestration"]["client"]["endpoints"]["query"]["href"]
        headers = {
            "Authorization": token,
            "X-BAMSDK-Platform-Id": self.config["device"]["platform_id"]
        }
        payload = {
            "operationName": "requestOtp",
            "variables": {
                "input": {
                    "email": email,
                    "reason": "Login"
                }
            },
            "query": queries.REQUESET_OTP
        }
        data = self._request("POST", endpoint, payload=payload, headers=headers)
        if not data["data"]["requestOtp"]["accepted"]:
            self.log.error(" - OTP code request failed.", exc_info=False)
            sys.exit(1)

    def _auth_action_with_otp(self, email: str, otp: str, token: str) -> dict:
        endpoint = self.prod_config["services"]["orchestration"]["client"]["endpoints"]["query"]["href"]
        headers = {
            "Authorization": token,
            "X-BAMSDK-Platform-Id": self.config["device"]["platform_id"]
        }
        payload = {
            "operationName": "authenticateWithOtp",
            "variables": {
                "input": {
                    "email": email,
                    "passcode": otp
                }
            },
            "query": queries.LOGIN_OTP
        }
        data = self._request("POST", endpoint, payload=payload, headers=headers)
        return data["data"]["authenticateWithOtp"]["actionGrant"]
    
    def _login_with_auth_action(self, auth_action: str, token: str) -> dict:
        endpoint = self.prod_config["services"]["orchestration"]["client"]["endpoints"]["query"]["href"]
        headers = {
            "Authorization": token,
            "X-BAMSDK-Platform-Id": self.config["device"]["platform_id"]
        }
        payload = {
            "operationName": "loginWithActionGrant",
            "variables": {
                "input": {
                    "actionGrant": auth_action
                },
                "includeAccountConsentToken": True
            },
            "query": queries.LOGIN_ACTION_GRANT
        }
        data = self._request("POST", endpoint, payload=payload, headers=headers)
        return data["extensions"]["sdk"]
    
    def _get_account_info(self, headers: dict = {}) -> dict:
        endpoint = self.prod_config["services"]["orchestration"]["client"]["endpoints"]["query"]["href"]
        headers.update({"X-BAMSDK-Platform-Id": self.config["device"]["platform_id"]})
        payload = {
            "operationName": "me",
            "variables": {
                "includeAccountConsentToken": True
            },
            "query": queries.ME
        }
        data = self._request("POST", endpoint, payload=payload, headers=headers)
        return data["data"]["me"]

    def _switch_profile(self, profile_id: str, headers: dict, pin: str = None):
        profile_input = {"profileId": profile_id}
        if pin: profile_input["entryPin"] = pin

        endpoint = self.prod_config["services"]["orchestration"]["client"]["endpoints"]["query"]["href"]
        headers.update({"X-BAMSDK-Platform-Id": self.config["device"]["platform_id"]})
        payload = {
            "operationName": "switchProfile",
            "variables": {
                "input": profile_input,
                "includeIdentity": True,
                "includeAccountConsentToken": True
            },
            "query": queries.SWITCH_PROFILE
        }
        data = self._request("POST", endpoint, payload=payload, headers=headers)
        return data["extensions"]["sdk"]

    def _refresh_token(self, refresh_token: str) -> dict:
        endpoint = self.prod_config["services"]["orchestration"]["client"]["endpoints"]["refreshToken"]["href"]
        headers = {
            "Authorization": self.config["bamsdk"]["api_key"],
            "X-BAMSDK-Platform-Id": self.config["device"]["platform_id"]
        }
        payload = {
            "operationName": "refreshToken",
            "variables": {
                "refreshToken": {
                    "refreshToken": refresh_token
                }
            },
            "query": queries.REFRESH_TOKEN
        }
        data = self._request("POST", endpoint, payload=payload, headers=headers)
        return data["extensions"]["sdk"]

    def _update_device(self, android_id: str, drm_id: str) -> str:
        endpoint = self.prod_config["services"]["orchestration"]["client"]["endpoints"]["query"]["href"]
        headers = {"X-BAMSDK-Platform-Id": self.config["device"]["platform_id"]}
        payload = {
            "operationName": "updateDeviceOperatingSystem",
            "variables": {
                "updateDeviceOperatingSystem": {
                    "operatingSystem": self.config["device"]["operatingSystem"],
                    "operatingSystemVersion": self.config["device"]["operatingSystemVersion"],
                    "osDeviceIds": [
                        {
                            "identifier": android_id,
                            "type": "android.vendor.id"
                        },
                        {
                            "identifier": drm_id,
                            "type": "android.drm.id"
                        }
                    ]
                }
            },
            "query": queries.UPDATE_DEVICE
        }
        data = self._request("POST", endpoint, payload=payload, headers=headers)

        if data["data"]["updateDeviceOperatingSystem"]["accepted"]:
            return data["extensions"]["sdk"]
        else:
            self.log.warning("   - Failed to update Device Operating System.")

    def _set_imax_preference(self, enabled: bool) -> str:
        endpoint = self.prod_config["services"]["orchestration"]["client"]["endpoints"]["query"]["href"]
        headers = {"X-BAMSDK-Platform-Id": self.config["device"]["platform_id"]}
        payload = {
            "operationName": "updateProfileImaxEnhancedVersion",
            "variables": {
                "input": {
                    "imaxEnhancedVersion": enabled,
                },
                "includeProfile": True
            },
            "query": queries.SET_IMAX,
        }
        data = self._request("POST", endpoint, payload=payload, headers=headers)
        
        if data["data"]["updateProfileImaxEnhancedVersion"]["accepted"]:
            self.log.info(f"  + Updated IMAX Enhanced preference: {enabled}")
            return data["extensions"]["sdk"]
        else:
            self.log.warning("  - Failed to set IMAX preference.")

    def _set_remastered_ar_preference(self, enabled: bool) -> str:
        endpoint = self.prod_config["services"]["orchestration"]["client"]["endpoints"]["query"]["href"]
        headers = {"X-BAMSDK-Platform-Id": self.config["device"]["platform_id"]}
        payload = {
            "operationName": "updateProfileRemasteredAspectRatio",
            "variables": {
                "input": {
                    "remasteredAspectRatio": enabled,
                },
                "includeProfile": True
            },
            "query": queries.SET_REMASTERED_AR,
        }
        data = self._request("POST", endpoint, payload=payload, headers=headers)
        
        if data["data"]["updateProfileRemasteredAspectRatio"]["accepted"]:
            self.log.info(f"  + Updated Remastered Aspect Ratio preference: {enabled}")
            return data["extensions"]["sdk"]
        else:
            self.log.warning("  - Failed to set Remastered Aspect Ratio preference.")

    def _href(self, href: str, **kwargs: Any) -> str:
        _args = {"version": self.config["bamsdk"]["explore_version"]}
        _args.update(**kwargs)
        return href.format(**_args)
    
    def _request(self, method: str, endpoint: str, params: dict = None, headers: dict = None, payload: dict = None) -> Any[dict | str]:
        _headers = self.session.headers.copy()
        if headers: _headers.update(headers)
        _headers.update({
            "X-BAMSDK-Transaction-ID": str(uuid.uuid4()),
            "X-Request-ID": str(uuid.uuid4())
        })

        req = Request(method, endpoint, headers=_headers, params=params, json=payload)
        prepped = self.session.prepare_request(req)
        try:
            res = self.session.send(prepped)
            res.raise_for_status()
            data = res.json()
            if data.get("errors"):
                error_code = data["errors"][0]["extensions"]["code"]
                if "token.service.invalid.grant" in error_code:
                    raise ConnectionError(f"Refresh Token Expired: {error_code}")
                elif "token.service.unauthorized.client" in error_code:
                    raise ConnectionError(f"Unauthorized Client/IP: {error_code}")
                elif "idp.error.identity.bad-credentials" in error_code:
                    raise ConnectionError(f"Bad Credentials: {error_code}")
                elif "account.profile.pin.invalid" in error_code:
                    raise ConnectionError(f"Invalid PIN: {error_code}")
                raise ConnectionError(data["errors"])
            if data.get("data") and data["data"].get("errors"):
                raise ConnectionError(data["data"]["errors"])
            return data
        except Exception as e:
            if "Refresh Token Expired" in str(e) or "/deeplink" in endpoint:
                raise e
            else:
                self.log.error(f"API Request failed: {e}", exc_info=False)
                sys.exit(1)