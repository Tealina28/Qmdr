from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import aiohttp
from qqmusic_api import songlist, user
from qqmusic_api.login import Credential

from .models import PlaylistItem, SongItem
from .music import MusicService
from .utils import ensure_directory, sanitize_filename


class CredentialRequiredError(Exception):
    pass


class PlaylistLinkError(ValueError):
    """歌单链接无法识别。"""


# 前两类链接与参考项目 QMDown 的 extractor/songlist.py 保持一致（并兼容新版 /n/ryqq_v2/ 路径），
# 另外接受纯数字歌单 ID。
_PLAYLIST_PATH_RE = re.compile(r"^/n/ryqq(?:_v\d+)?/playlist/(?P<id>\d+)/?$")
_TAOGE_PATH_RE = re.compile(r"^/n2/m/share/details/taoge\.html$")
_SHARE_PATH_RE = re.compile(r"^/base/fcgi-bin/u$")
_DIGITS_RE = re.compile(r"^\d+$")
_ID_QUERY_KEYS = ("id", "disstid", "dissid")
# QQ 音乐官方域名白名单，取自参考项目 musicdl 的 QQ_MUSIC_HOSTS
# (musicdl/modules/utils/hosts.py)。用精确白名单而不是 ".qq.com" 通配：qq.com 下还有
# 大量与音乐无关的子域（例如 news.qq.com），只认已知的音乐域名更安全。
_QQ_MUSIC_HOSTS = frozenset(
    {
        "y.qq.com",
        "i.y.qq.com",
        "m.y.qq.com",
        "c.y.qq.com",
        "c6.y.qq.com",
        "music.qq.com",
        "qq.com",
    }
)


def parse_playlist_id(text: str) -> int:
    """从歌单链接或纯数字 ID 中取出歌单 ID。

    支持以下形式（查询参数如 ADTAG 会被忽略）：
    - https://y.qq.com/n/ryqq/playlist/<歌单ID>
    - https://y.qq.com/n/ryqq_v2/playlist/<歌单ID>?ADTAG=h5_share_playlist
    - https://i.y.qq.com/n2/m/share/details/taoge.html?id=<歌单ID>
    """
    value = (text or "").strip()
    if not value:
        raise PlaylistLinkError("请输入歌单链接或歌单 ID")

    if _DIGITS_RE.match(value):
        return _positive_id(value)

    parts = _split(value)
    host = _host_of(parts)
    if not _is_qq_music_host(host):
        raise PlaylistLinkError("仅支持 QQ 音乐（qq.com）的歌单链接")

    path = _path_of(parts)
    if match := _PLAYLIST_PATH_RE.match(path):
        return _positive_id(match.group("id"))

    if _TAOGE_PATH_RE.match(path):
        query = parse_qs(parts.query)
        for key in _ID_QUERY_KEYS:
            for raw in query.get(key, []):
                if _DIGITS_RE.match(raw.strip()):
                    return _positive_id(raw.strip())

    raise PlaylistLinkError("无法从链接中解析出歌单 ID，请检查是否为 QQ 音乐歌单链接")


def is_share_link(text: str) -> bool:
    """判断是否为需要跟随跳转才能拿到歌单 ID 的分享短链。"""
    try:
        parts = _split(text)
    except PlaylistLinkError:
        return False
    if not _is_qq_music_host(_host_of(parts)):
        return False
    return bool(_SHARE_PATH_RE.match(_path_of(parts)))


def _is_qq_music_host(host: str) -> bool:
    return host in _QQ_MUSIC_HOSTS


def _split(value: str):
    candidate = value if "://" in value else f"https://{value}"
    parts = urlsplit(candidate)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise PlaylistLinkError("无法识别的歌单链接")
    return parts


def _host_of(parts) -> str:
    return (parts.hostname or "").lower()


def _path_of(parts) -> str:
    return parts.path.rstrip("/") or "/"


def _positive_id(raw: str) -> int:
    playlist_id = int(raw)
    if playlist_id <= 0:
        raise PlaylistLinkError("歌单 ID 必须大于 0")
    return playlist_id


class PlaylistService:
    def __init__(self, music_service: MusicService) -> None:
        self.music_service = music_service

    async def get_user_playlists(
        self,
        user_id: str,
        credential: Credential | None,
    ) -> list[PlaylistItem]:
        if credential is None:
            raise CredentialRequiredError("歌单下载需要先登录")
        items = await user.get_created_songlist(user_id, credential=credential)
        return [self.playlist_from_raw(item) for item in items or []]

    def playlist_from_raw(self, data: dict) -> PlaylistItem:
        return PlaylistItem(
            name=data.get("dirName", "未知歌单"),
            dir_id=int(data.get("dirId", 0) or 0),
            tid=int(data.get("tid", 0) or 0),
            song_count=int(data.get("songNum", 0) or 0),
            raw=data,
        )

    async def get_playlist_songs(
        self,
        playlist: PlaylistItem,
        user_id: str,
        credential: Credential | None,
    ) -> list[SongItem]:
        if credential is None:
            raise CredentialRequiredError("歌单下载需要先登录")
        if playlist.dir_id == 201 and self._is_other_user(user_id, credential):
            raise PermissionError("'我喜欢' 歌单不公开，无法下载其他用户的该歌单")
        return await self._fetch_songs(playlist.tid, playlist.dir_id)

    async def fetch_playlist_by_link(self, link: str) -> PlaylistItem:
        """按歌单链接取出歌单信息，公开歌单无需登录。"""
        playlist_id = parse_playlist_id(link)
        detail = await songlist.get_detail(songlist_id=playlist_id, num=1)
        songs = detail.get("songlist") or []
        info = detail.get("dirinfo") or {}
        creator = info.get("creator") or {}
        return PlaylistItem(
            name=info.get("title") or info.get("dissname") or f"歌单 {playlist_id}",
            # 取歌只取决于 tid，dirid 传 0 即可；dirinfo.dirid 对第三方歌单是自己的 ID，
            # 对“我喜欢”则是 201（表示私有），这里不沿用，避免误判为他人私有歌单。
            dir_id=0,
            tid=playlist_id,
            song_count=int(
                info.get("songnum") or detail.get("total_song_num") or detail.get("songlist_size") or len(songs)
            ),
            owner_id=str(creator.get("musicid") or creator.get("uin") or creator.get("encrypt_uin") or ""),
            owner_name=creator.get("nick") or creator.get("name") or "",
            raw=info,
        )

    async def get_link_playlist_songs(
        self,
        playlist: PlaylistItem,
        credential: Credential | None,
    ) -> list[SongItem]:
        """按歌单链接取歌，公开歌单无需登录；失败时提示登录后重试。"""
        try:
            songs = await self._fetch_songs(playlist.tid, playlist.dir_id)
        except Exception as exc:  # noqa: BLE001 - 需要把网络错误转成可读提示
            if credential is None:
                raise CredentialRequiredError(
                    f"读取歌单失败，该歌单可能需要登录后才能访问。请在「设置」登录后重试。（{exc}）"
                ) from exc
            raise
        if not songs and playlist.song_count and credential is None:
            raise CredentialRequiredError("该歌单需要登录后才能访问。请在「设置」登录后重试。")
        return songs

    async def resolve_share_link(self, url: str) -> str:
        """跟随分享短链跳转，返回最终落地的链接。"""
        timeout = aiohttp.ClientTimeout(total=15)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as response:
                    return str(response.real_url)
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise PlaylistLinkError(f"短链解析失败: {exc}") from exc

    async def _fetch_songs(self, tid: int, dir_id: int) -> list[SongItem]:
        songs = await songlist.get_songlist(tid, dir_id)
        return [self.music_service.song_from_raw(item) for item in songs or []]

    def playlist_folder(self, base_dir: Path, playlist: PlaylistItem, user_id: str = "") -> Path:
        return ensure_directory(base_dir / sanitize_filename(playlist.name))

    @staticmethod
    def _is_other_user(user_id: str, credential: Credential) -> bool:
        return str(getattr(credential, "musicid", "")) != str(user_id)
