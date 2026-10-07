from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class DownloadOptions:
    download_dir: Path
    quality_level: int = 3
    cover_size: int = 800
    batch_size: int = 5
    overwrite: bool = False
    save_lyric_file: bool = False
    save_trans_lyric_file: bool = False


@dataclass(slots=True)
class SongItem:
    title: str
    singer: str
    mid: str
    album_name: str = ""
    album_mid: str = ""
    is_vip: bool = False
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def display_name(self) -> str:
        return f"{self.singer} - {self.title}"


@dataclass(slots=True)
class PlaylistItem:
    name: str
    dir_id: int
    tid: int
    song_count: int = 0
    owner_id: str = ""
    owner_name: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def source_label(self) -> str:
        return f"来自 {self.owner_name}" if self.owner_name else "链接歌单"


@dataclass(slots=True)
class DownloadEvent:
    kind: str
    message: str
    current: int = 0
    total: int = 0
    song: SongItem | None = None
    file_path: Path | None = None
    error: str | None = None


@dataclass(slots=True)
class DownloadResult:
    success: bool
    song: SongItem
    skipped: bool = False
    quality: str | None = None
    file_path: Path | None = None
    error: str | None = None


@dataclass(slots=True)
class CredentialState:
    loaded: bool
    refreshed: bool = False
    loaded_from_api: bool = False
    expired: bool = False
    path: Path | None = None
    user_id: str | None = None
    message: str = ""
    credential: Any = field(default=None, repr=False)


@dataclass(slots=True)
class CredentialStatus:
    exists: bool
    expired: bool | None = None
    can_refresh: bool | None = None
    user_id: str | None = None
    path: Path | None = None
    message: str = ""


@dataclass(slots=True)
class QRLoginSession:
    login_type: str
    image_bytes: bytes
    qr: Any = field(repr=False)


@dataclass(slots=True)
class QRLoginPollResult:
    event_name: str
    done: bool = False
    failed: bool = False
    message: str = ""
    credential: Any = field(default=None, repr=False)
