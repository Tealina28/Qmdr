from __future__ import annotations

from pathlib import Path

from qqmusic_api import songlist, user
from qqmusic_api.login import Credential

from .models import PlaylistItem, SongItem
from .music import MusicService
from .utils import ensure_directory, sanitize_filename


class CredentialRequiredError(Exception):
    pass


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
        songs = await songlist.get_songlist(playlist.tid, playlist.dir_id)
        return [self.music_service.song_from_raw(item) for item in songs or []]

    def playlist_folder(self, base_dir: Path, playlist: PlaylistItem, user_id: str) -> Path:
        return ensure_directory(base_dir / sanitize_filename(playlist.name))

    @staticmethod
    def _is_other_user(user_id: str, credential: Credential) -> bool:
        return str(getattr(credential, "musicid", "")) != str(user_id)
