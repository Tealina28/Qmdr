from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

import aiofiles
import aiohttp
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3, TALB, TIT2, TPE1, USLT
from mutagen.mp4 import MP4, MP4Cover
from qqmusic_api import search
from qqmusic_api.login import Credential
from qqmusic_api.lyric import get_lyric
from qqmusic_api.song import SongFileType, get_song_urls

from .models import DownloadEvent, DownloadOptions, DownloadResult, SongItem
from .quality import get_quality_strategy
from .settings import DOWNLOAD_TIMEOUT, MIN_FILE_SIZE, SEARCH_RESULTS_COUNT
from .utils import emit_event, ensure_directory, sanitize_filename

DownloadCallback = Callable[[DownloadEvent], Awaitable[None] | None]


class DownloadError(Exception):
    pass


class MetadataError(Exception):
    pass


class NetworkManager:
    def __init__(self, timeout: int = DOWNLOAD_TIMEOUT) -> None:
        self.timeout = timeout
        self.session: aiohttp.ClientSession | None = None

    async def get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            self.session = aiohttp.ClientSession(timeout=timeout)
        return self.session

    async def close(self) -> None:
        if self.session is not None and not self.session.closed:
            await self.session.close()
        self.session = None


class CoverManager:
    @staticmethod
    def get_cover_url_by_album_mid(mid: str, size: int = 800) -> str | None:
        if not mid:
            return None
        if size not in {150, 300, 500, 800}:
            raise ValueError("不支持的封面尺寸")
        return f"https://y.gtimg.cn/music/photo_new/T002R{size}x{size}M000{mid}.jpg"

    @staticmethod
    def get_cover_url_by_vs(vs: str, size: int = 800) -> str | None:
        if not vs:
            return None
        if size not in {150, 300, 500, 800}:
            raise ValueError("不支持的封面尺寸")
        return f"https://y.qq.com/music/photo_new/T062R{size}x{size}M000{vs}.jpg"

    @staticmethod
    async def get_valid_cover_url(
        song_data: dict[str, Any],
        network: NetworkManager,
        size: int = 800,
    ) -> str | None:
        cover = await CoverManager.get_valid_cover(song_data, network, size)
        return cover[0] if cover else None

    @staticmethod
    async def get_valid_cover(
        song_data: dict[str, Any],
        network: NetworkManager,
        size: int = 800,
    ) -> tuple[str, bytes] | None:
        album_mid = song_data.get("album", {}).get("mid", "")
        if album_mid:
            url = CoverManager.get_cover_url_by_album_mid(album_mid, size)
            cover_data = await CoverManager.download_cover(url, network)
            if cover_data:
                return url, cover_data

        candidates: list[tuple[int, str]] = []
        for i, vs in enumerate(song_data.get("vs", [])):
            if vs and isinstance(vs, str) and len(vs) >= 3 and "," not in vs:
                candidates.append((i, vs))
        for i, vs in enumerate(song_data.get("vs", [])):
            if vs and isinstance(vs, str) and "," in vs:
                for part in (item.strip() for item in vs.split(",")):
                    if len(part) >= 3:
                        candidates.append((100 + i, part))

        for _, value in sorted(candidates, key=lambda item: item[0]):
            url = CoverManager.get_cover_url_by_vs(value, size)
            cover_data = await CoverManager.download_cover(url, network)
            if cover_data:
                return url, cover_data
        return None

    @staticmethod
    async def download_cover(url: str | None, network: NetworkManager) -> bytes | None:
        if not url:
            return None
        try:
            session = await network.get_session()
            async with session.get(url) as response:
                if response.status != 200:
                    return None
                content = await response.read()
        except (aiohttp.ClientError, TimeoutError):
            return None
        if len(content) <= MIN_FILE_SIZE:
            return None
        if content.startswith(b"\xff\xd8") or content.startswith(b"\x89PNG"):
            return content
        return None


class MetadataManager:
    def __init__(self, network: NetworkManager) -> None:
        self.network = network

    async def add_metadata(
        self,
        file_path: Path,
        song: SongItem,
        song_data: dict[str, Any],
        cover_size: int,
        options: DownloadOptions | None = None,
        on_event: DownloadCallback | None = None,
        current: int = 1,
        total: int = 1,
    ) -> None:
        lyrics_data = await self.get_lyrics(song.mid)
        suffix = file_path.suffix.lower()
        if suffix == ".flac":
            await self._add_metadata_to_flac(file_path, song, lyrics_data, song_data, cover_size)
        elif suffix == ".mp3":
            await self._add_metadata_to_mp3(file_path, song, lyrics_data, song_data, cover_size)
        elif suffix == ".m4a":
            await self._add_metadata_to_mp4(file_path, song, lyrics_data, song_data, cover_size)

        if options is None or not lyrics_data:
            return
        if not (lyrics_data.get("lyric") or lyrics_data.get("trans")):
            return
        written = self.write_lyrics_files(file_path, lyrics_data, options)
        if written:
            await emit_event(
                on_event,
                DownloadEvent(
                    kind="info",
                    message=f"已保存歌词文件: {'、'.join(path.name for path in written)}",
                    current=current,
                    total=total,
                    song=song,
                    file_path=file_path,
                ),
            )

    async def _add_metadata_to_flac(
        self,
        file_path: Path,
        song: SongItem,
        lyrics_data: dict[str, Any] | None,
        song_data: dict[str, Any],
        cover_size: int,
    ) -> None:
        try:
            audio = FLAC(file_path)
            audio["title"] = song.title
            audio["artist"] = song.singer
            audio["album"] = song.album_name
            cover = await CoverManager.get_valid_cover(song_data, self.network, cover_size)
            if cover:
                _, cover_data = cover
                image = Picture()
                image.type = 3
                image.mime = "image/png" if cover_data.startswith(b"\x89PNG") else "image/jpeg"
                image.desc = "Cover"
                image.data = cover_data
                audio.clear_pictures()
                audio.add_picture(image)
            if lyrics_data:
                if lyric_text := lyrics_data.get("lyric"):
                    audio["lyrics"] = lyric_text
                if trans_text := lyrics_data.get("trans"):
                    audio["translyrics"] = trans_text
            audio.save()
        except Exception as exc:  # noqa: BLE001
            raise MetadataError(f"FLAC 元数据处理失败: {exc}") from exc

    async def _add_metadata_to_mp3(
        self,
        file_path: Path,
        song: SongItem,
        lyrics_data: dict[str, Any] | None,
        song_data: dict[str, Any],
        cover_size: int,
    ) -> None:
        try:
            try:
                audio = ID3(file_path)
            except Exception:
                audio = ID3()
            for tag in ["APIC:", "USLT:", "TIT2", "TPE1", "TALB"]:
                if tag in audio:
                    del audio[tag]
            audio.add(TIT2(encoding=3, text=song.title))
            audio.add(TPE1(encoding=3, text=song.singer))
            audio.add(TALB(encoding=3, text=song.album_name))
            cover = await CoverManager.get_valid_cover(song_data, self.network, cover_size)
            if cover:
                _, cover_data = cover
                audio.add(
                    APIC(
                        encoding=3,
                        mime="image/png" if cover_data.startswith(b"\x89PNG") else "image/jpeg",
                        type=3,
                        desc="Cover",
                        data=cover_data,
                    )
                )
            if lyrics_data and (lyric_text := lyrics_data.get("lyric")):
                audio.add(USLT(encoding=3, lang="eng", desc="Lyrics", text=lyric_text))
            audio.save(file_path, v2_version=3)
        except Exception as exc:  # noqa: BLE001
            raise MetadataError(f"MP3 元数据处理失败: {exc}") from exc

    async def _add_metadata_to_mp4(
        self,
        file_path: Path,
        song: SongItem,
        lyrics_data: dict[str, Any] | None,
        song_data: dict[str, Any],
        cover_size: int,
    ) -> None:
        try:
            audio = MP4(file_path)
            audio["\xa9nam"] = [song.title]
            audio["\xa9ART"] = [song.singer]
            if song.album_name:
                audio["\xa9alb"] = [song.album_name]
            cover = await CoverManager.get_valid_cover(song_data, self.network, cover_size)
            if cover:
                _, cover_data = cover
                image_format = MP4Cover.FORMAT_PNG if cover_data.startswith(b"\x89PNG") else MP4Cover.FORMAT_JPEG
                audio["covr"] = [MP4Cover(cover_data, imageformat=image_format)]
            if lyrics_data and (lyric_text := lyrics_data.get("lyric")):
                audio["\xa9lyr"] = [lyric_text]
            audio.save()
        except Exception as exc:  # noqa: BLE001
            raise MetadataError(f"MP4 元数据处理失败: {exc}") from exc

    async def get_lyrics(self, song_mid: str) -> dict[str, Any] | None:
        try:
            return await get_lyric(song_mid)
        except Exception:  # noqa: BLE001
            return None

    def write_lyrics_files(
        self,
        file_path: Path,
        lyrics_data: dict[str, Any],
        options: DownloadOptions,
    ) -> list[Path]:
        """按选项写出独立歌词文件，返回实际写入的文件列表。

        `.lrc` 与音频同名同目录；整首校对歌词与逐行翻译分别写各自后缀。
        """
        written: list[Path] = []
        lyric_text = self._clean_lyric(lyrics_data.get("lyric"))
        trans_text = self._clean_lyric(lyrics_data.get("trans"))
        if options.save_lyric_file and lyric_text:
            target = file_path.with_suffix(".lrc")
            # 双语歌词是通行做法：原文在上，空行后接翻译。
            body = f"{lyric_text}\n\n{trans_text}\n" if trans_text else lyric_text
            if self._save_lyric_file(target, body, options):
                written.append(target)
        if options.save_trans_lyric_file and trans_text:
            target = file_path.with_suffix(".trans.lrc")
            if self._save_lyric_file(target, trans_text, options):
                written.append(target)
        return written

    @staticmethod
    def _clean_lyric(text: Any) -> str:
        return text.strip() if isinstance(text, str) else ""

    @staticmethod
    def _save_lyric_file(target: Path, text: str, options: DownloadOptions) -> bool:
        if target.exists() and not options.overwrite:
            return False
        try:
            ensure_directory(target.parent)
            # utf-8-sig：大量播放器按本地代码页读 .lrc，带 BOM 才不会把中文解成乱码。
            target.write_text(text if text.endswith("\n") else f"{text}\n", encoding="utf-8-sig")
        except OSError:
            return False
        return True

    @staticmethod
    def lyric_file_paths(file_path: Path, options: DownloadOptions) -> list[Path]:
        """返回该音频按选项应当存在的歌词文件路径，用于跳过判断。"""
        paths: list[Path] = []
        if options.save_lyric_file:
            paths.append(file_path.with_suffix(".lrc"))
        if options.save_trans_lyric_file:
            paths.append(file_path.with_suffix(".trans.lrc"))
        return paths

    @staticmethod
    def has_lyric_files(file_path: Path, options: DownloadOptions) -> bool:
        return all(path.exists() for path in MetadataManager.lyric_file_paths(file_path, options))


class MusicService:
    def __init__(self, network: NetworkManager | None = None) -> None:
        self.network = network or NetworkManager()
        self.metadata = MetadataManager(self.network)

    async def close(self) -> None:
        await self.network.close()

    async def search_songs(self, keyword: str, count: int = SEARCH_RESULTS_COUNT) -> list[SongItem]:
        keyword = keyword.strip()
        if not keyword:
            raise ValueError("搜索关键词不能为空")
        results = await search.search_by_type(keyword, num=count)
        return [self.song_from_raw(item) for item in results or []]

    def song_from_raw(self, data: dict[str, Any]) -> SongItem:
        album = data.get("album") or {}
        if not isinstance(album, dict):
            album = {}
        pay = data.get("pay") or {}
        if not isinstance(pay, dict):
            pay = {}
        mid = data.get("mid") or data.get("songmid") or ""
        return SongItem(
            title=data.get("title") or data.get("name") or data.get("songname") or "未知歌曲",
            singer=self._singer_from_raw(data.get("singer")),
            mid=mid,
            album_name=album.get("name") or data.get("albumname") or "",
            album_mid=album.get("mid") or "",
            is_vip=pay.get("pay_play", 0) != 0,
            raw=data,
        )

    @staticmethod
    def _singer_from_raw(singer_info: Any) -> str:
        if isinstance(singer_info, str):
            return singer_info or "未知歌手"
        if isinstance(singer_info, list) and singer_info:
            first = singer_info[0]
            if isinstance(first, dict):
                return first.get("name") or "未知歌手"
            if isinstance(first, str):
                return first or "未知歌手"
        return "未知歌手"

    async def download_song(
        self,
        song: SongItem,
        options: DownloadOptions,
        credential: Credential | None = None,
        on_event: DownloadCallback | None = None,
        folder: Path | None = None,
        current: int = 1,
        total: int = 1,
    ) -> DownloadResult:
        target_dir = ensure_directory(folder or options.download_dir)
        safe_name = sanitize_filename(song.display_name)

        if song.is_vip and credential is None:
            await emit_event(
                on_event,
                DownloadEvent(
                    kind="warning",
                    message=f"{song.display_name} 是 VIP 歌曲，未登录时可能只能下载低音质或失败",
                    current=current,
                    total=total,
                    song=song,
                ),
            )

        last_error = "所有音质下载失败"
        for file_type, quality_name in get_quality_strategy(options.quality_level):
            file_path = target_dir / f"{safe_name}{file_type.e}"
            if file_path.exists() and not options.overwrite:
                # 音频已在，永不重下；只在开启了独立歌词选项且歌词缺失时补写歌词。
                if not self.metadata.has_lyric_files(file_path, options):
                    await self._backfill_lyric_files(file_path, song, options, on_event, current, total)
                await emit_event(
                    on_event,
                    DownloadEvent(
                        kind="skipped",
                        message=f"文件已存在，跳过: {file_path.name}",
                        current=current,
                        total=total,
                        song=song,
                        file_path=file_path,
                    ),
                )
                return DownloadResult(True, song=song, skipped=True, quality=quality_name, file_path=file_path)

            await emit_event(
                on_event,
                DownloadEvent(
                    kind="downloading",
                    message=f"尝试 {quality_name}: {song.display_name}",
                    current=current,
                    total=total,
                    song=song,
                    file_path=file_path,
                ),
            )
            result = await self._download_with_quality(
                song=song,
                file_type=file_type,
                quality_name=quality_name,
                file_path=file_path,
                credential=credential,
                options=options,
                on_event=on_event,
                current=current,
                total=total,
            )
            if result.success:
                return result
            last_error = result.error or last_error

        await emit_event(
            on_event,
            DownloadEvent(
                kind="failed",
                message=f"下载失败: {song.display_name} - {last_error}",
                current=current,
                total=total,
                song=song,
                error=last_error,
            ),
        )
        return DownloadResult(False, song=song, error=last_error)

    async def _download_with_quality(
        self,
        song: SongItem,
        file_type: SongFileType,
        quality_name: str,
        file_path: Path,
        credential: Credential | None,
        options: DownloadOptions,
        on_event: DownloadCallback | None,
        current: int,
        total: int,
    ) -> DownloadResult:
        try:
            urls = await get_song_urls([song.mid], file_type=file_type, credential=credential)
        except Exception as exc:  # noqa: BLE001
            return DownloadResult(False, song=song, quality=quality_name, error=f"获取 URL 失败: {exc}")

        url = urls.get(song.mid)
        if isinstance(url, tuple):
            url = url[0]
        if not url:
            await emit_event(
                on_event,
                DownloadEvent(
                    kind="fallback",
                    message=f"{quality_name} 不可用，尝试降级",
                    current=current,
                    total=total,
                    song=song,
                ),
            )
            return DownloadResult(False, song=song, quality=quality_name, error=f"无法获取 {quality_name} URL")

        try:
            session = await self.network.get_session()
            async with session.get(url) as response:
                if response.status != 200:
                    return DownloadResult(False, song=song, quality=quality_name, error=f"HTTP {response.status}")
                ensure_directory(file_path.parent)
                file_size = await self._save_response_to_file(response, file_path, on_event, current, total, song)
        except (aiohttp.ClientError, TimeoutError, OSError) as exc:
            return DownloadResult(False, song=song, quality=quality_name, error=f"下载或写入失败: {exc}")

        if file_size <= MIN_FILE_SIZE:
            with suppress(OSError):
                file_path.unlink()
            return DownloadResult(False, song=song, quality=quality_name, error="文件过小，可能下载失败")

        try:
            await self.metadata.add_metadata(
                file_path,
                song,
                song.raw,
                options.cover_size,
                options=options,
                on_event=on_event,
                current=current,
                total=total,
            )
        except MetadataError as exc:
            await emit_event(
                on_event,
                DownloadEvent(
                    kind="warning",
                    message=f"元数据写入失败，但音频已保存: {exc}",
                    current=current,
                    total=total,
                    song=song,
                    file_path=file_path,
                    error=str(exc),
                ),
            )

        await emit_event(
            on_event,
            DownloadEvent(
                kind="success",
                message=f"下载成功: {file_path.name}",
                current=current,
                total=total,
                song=song,
                file_path=file_path,
            ),
        )
        return DownloadResult(True, song=song, quality=quality_name, file_path=file_path)

    async def _backfill_lyric_files(
        self,
        file_path: Path,
        song: SongItem,
        options: DownloadOptions,
        on_event: DownloadCallback | None,
        current: int,
        total: int,
    ) -> None:
        """音频已存在但缺少歌词文件时补写歌词；无歌词可用时静默跳过。"""
        lyrics_data = await self.metadata.get_lyrics(song.mid)
        if not (lyrics_data and lyrics_data.get("lyric")):
            return
        written = self.metadata.write_lyrics_files(file_path, lyrics_data, options)
        if not written:
            return
        await emit_event(
            on_event,
            DownloadEvent(
                kind="info",
                message=f"音频已存在，补充歌词文件: {'、'.join(path.name for path in written)}",
                current=current,
                total=total,
                song=song,
                file_path=file_path,
            ),
        )

    async def _save_response_to_file(
        self,
        response: aiohttp.ClientResponse,
        file_path: Path,
        on_event: DownloadCallback | None,
        current: int,
        total: int,
        song: SongItem,
    ) -> int:
        temp_path = file_path.with_name(f"{file_path.name}.part")
        downloaded = 0
        try:
            content_length = int(response.headers.get("Content-Length") or 0)
        except ValueError:
            content_length = 0
        next_report = 0.1
        try:
            async with aiofiles.open(temp_path, "wb") as fp:
                async for chunk in response.content.iter_chunked(256 * 1024):
                    if not chunk:
                        continue
                    await fp.write(chunk)
                    downloaded += len(chunk)
                    if content_length:
                        progress = min(1.0, downloaded / content_length)
                        if progress >= next_report or progress >= 1.0:
                            await emit_event(
                                on_event,
                                DownloadEvent(
                                    kind="file_progress",
                                    message=f"{song.display_name} 下载中 {progress:.0%}",
                                    current=current,
                                    total=total,
                                    song=song,
                                    file_path=file_path,
                                ),
                            )
                            while next_report <= progress:
                                next_report += 0.1
            temp_path.replace(file_path)
        except Exception:
            with suppress(OSError):
                temp_path.unlink()
            raise
        return downloaded
