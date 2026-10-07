from __future__ import annotations

import asyncio
import json
import pickle
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from qqmusic_api.login import Credential
from qqmusic_api.song import SongFileType

from qmdr.coordinator import DownloadCoordinator
from qmdr.credential_service import CredentialService
from qmdr.models import DownloadEvent, DownloadOptions, DownloadResult, PlaylistItem, SongItem
from qmdr.music import MusicService
from qmdr.playlist import (
    CredentialRequiredError,
    PlaylistLinkError,
    PlaylistService,
    is_share_link,
    parse_playlist_id,
)
from qmdr.quality import get_quality_strategy
from qmdr.settings import load_download_dir, save_download_dir
from qmdr.utils import sanitize_filename


class CoreTests(unittest.TestCase):
    def test_sanitize_filename_replaces_invalid_chars(self) -> None:
        self.assertEqual(sanitize_filename('A<B>C:D"E/F\\G|H?I*'), "A_B_C_D_E_F_G_H_I_")

    def test_sanitize_filename_handles_windows_reserved_names(self) -> None:
        self.assertEqual(sanitize_filename("CON"), "_CON")
        self.assertEqual(sanitize_filename("name. "), "name")

    def test_quality_strategy_falls_back_in_expected_order(self) -> None:
        strategy = get_quality_strategy(1)
        self.assertEqual(strategy[0][0], SongFileType.MASTER)
        self.assertEqual(strategy[-1][0], SongFileType.MP3_128)

    def test_credential_candidates_prefer_app_data_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            primary = root / "app" / "qqmusic_cred.pkl"
            legacy = root / "qqmusic_cred.pkl"
            service = CredentialService(credential_path=primary, legacy_path=legacy)
            self.assertEqual(service.candidate_paths(), [primary, legacy])

    def test_credential_export_writes_raw_secret_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            credential_path = root / "qqmusic_cred.pkl"
            export_path = root / "credential.json"
            credential = Credential(
                openid="openid-value",
                refresh_token="refresh-token-value",
                access_token="access-token-value",
                expired_at=123,
                musicid=456,
                musickey="music-key-value",
                unionid="unionid-value",
                str_musicid="456",
                refresh_key="refresh-key-value",
                encrypt_uin="encrypt-uin-value",
                login_type=2,
            )
            with credential_path.open("wb") as fp:
                pickle.dump(credential, fp)

            service = CredentialService(credential_path=credential_path, legacy_path=credential_path)
            service.export_credential_to_json_file(export_path)

            data = json.loads(export_path.read_text(encoding="utf-8"))
            self.assertEqual(data["access_token"], "access-token-value")
            self.assertEqual(data["refresh_token"], "refresh-token-value")
            self.assertEqual(data["musickey"], "music-key-value")
            self.assertEqual(data["refresh_key"], "refresh-key-value")

    def test_download_dir_setting_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings_path = root / "settings.json"
            download_dir = root / "downloads"

            save_download_dir(download_dir, settings_path=settings_path)

            self.assertEqual(load_download_dir(settings_path=settings_path), download_dir)

    def test_download_dir_setting_falls_back_on_invalid_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings_path = root / "settings.json"
            fallback = root / "fallback"
            settings_path.write_text("not-json", encoding="utf-8")

            self.assertEqual(load_download_dir(settings_path=settings_path, default=fallback), fallback)

    def test_existing_file_is_skipped_without_network(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                song = SongItem(title="Song", singer="Artist", mid="mid")
                existing = root / "Artist - Song.flac"
                existing.write_bytes(b"already here")
                events: list[DownloadEvent] = []
                service = MusicService()
                try:
                    result = await service.download_song(
                        song,
                        DownloadOptions(download_dir=root, quality_level=3),
                        on_event=events.append,
                    )
                finally:
                    await service.close()
                self.assertTrue(result.success)
                self.assertTrue(result.skipped)
                self.assertEqual(events[-1].kind, "skipped")

        asyncio.run(run())

    def test_download_url_failure_emits_failed_result(self) -> None:
        async def fake_get_song_urls(*args, **kwargs):  # noqa: ANN002, ANN003
            return {}

        async def run() -> None:
            with tempfile.TemporaryDirectory() as temp:
                events: list[DownloadEvent] = []
                service = MusicService()
                try:
                    with patch("qmdr.music.get_song_urls", fake_get_song_urls):
                        result = await service.download_song(
                            SongItem(title="Missing", singer="Artist", mid="missing"),
                            DownloadOptions(download_dir=Path(temp), quality_level=4),
                            on_event=events.append,
                        )
                finally:
                    await service.close()
                self.assertFalse(result.success)
                self.assertEqual(events[-1].kind, "failed")

        asyncio.run(run())

    def test_coordinator_counts_batch_results(self) -> None:
        class FakeMusicService:
            async def download_song(self, song, options, credential=None, on_event=None, folder=None, current=1, total=1):
                if on_event:
                    on_event(DownloadEvent(kind="success", message=song.title, current=current, total=total, song=song))
                return DownloadResult(True, song=song, file_path=Path(f"{song.title}.mp3"))

        async def run() -> None:
            songs = [SongItem(title=f"Song {index}", singer="Artist", mid=str(index)) for index in range(5)]
            events: list[DownloadEvent] = []
            coordinator = DownloadCoordinator(FakeMusicService())  # type: ignore[arg-type]
            results = await coordinator.download_songs(
                songs,
                DownloadOptions(download_dir=Path("."), batch_size=2),
                credential=None,
                on_event=events.append,
            )
            self.assertEqual(len(results), 5)
            self.assertTrue(all(item.success for item in results))
            self.assertEqual(events[-1].kind, "done")

        asyncio.run(run())

    def test_coordinator_preserves_cancelled_state_when_requested(self) -> None:
        class FakeMusicService:
            async def download_song(self, song, options, credential=None, on_event=None, folder=None, current=1, total=1):
                return DownloadResult(True, song=song)

        async def run() -> None:
            events: list[DownloadEvent] = []
            coordinator = DownloadCoordinator(FakeMusicService())  # type: ignore[arg-type]
            coordinator.cancel()
            results = await coordinator.download_songs(
                [SongItem(title="Song", singer="Artist", mid="1")],
                DownloadOptions(download_dir=Path(".")),
                credential=None,
                on_event=events.append,
                reset_cancel=False,
            )
            self.assertEqual(results, [])
            self.assertEqual(events[-1].kind, "cancelled")
            self.assertNotIn("done", [event.kind for event in events])

        asyncio.run(run())

    def test_coordinator_converts_task_exception_to_failed_result(self) -> None:
        class FakeMusicService:
            async def download_song(self, song, options, credential=None, on_event=None, folder=None, current=1, total=1):
                raise RuntimeError("boom")

        async def run() -> None:
            events: list[DownloadEvent] = []
            coordinator = DownloadCoordinator(FakeMusicService())  # type: ignore[arg-type]
            results = await coordinator.download_songs(
                [SongItem(title="Song", singer="Artist", mid="1")],
                DownloadOptions(download_dir=Path(".")),
                credential=None,
                on_event=events.append,
            )
            self.assertFalse(results[0].success)
            self.assertIn("failed", [event.kind for event in events])

        asyncio.run(run())

    def test_playlist_folder_uses_playlist_name_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            playlist = PlaylistItem(name="Daily", dir_id=123, tid=456)
            service = PlaylistService(MusicService())

            self.assertEqual(service.playlist_folder(root, playlist, "user"), root / "Daily")

    def test_parse_playlist_id_accepts_plain_id(self) -> None:
        # 占位 ID 故意用不可能是真实歌单的小号，避免把真实歌单 ID 写进仓库。
        self.assertEqual(parse_playlist_id("101"), 101)
        self.assertEqual(parse_playlist_id("  101  "), 101)

    def test_parse_playlist_id_accepts_supported_links(self) -> None:
        # 与参考项目 QMDown extractor/songlist.py 的 _VALID_URL 保持一致，并兼容新版 ryqq_v2 路径。
        cases = {
            "https://y.qq.com/n/ryqq/playlist/101": 101,
            "https://y.qq.com/n/ryqq/playlist/101/": 101,
            "http://y.qq.com/n/ryqq/playlist/101": 101,
            "https://y.qq.com/n/ryqq_v2/playlist/102": 102,
            "https://y.qq.com/n/ryqq_v2/playlist/102?ADTAG=h5_share_playlist&redirecttag=mn.redirect.custom&mnst=0.98": 102,
            "https://y.qq.com/n/ryqq_v2/playlist/102/": 102,
            "https://i.y.qq.com/n2/m/share/details/taoge.html?id=103": 103,
            "https://i.y.qq.com/n2/m/share/details/taoge.html?id=103&ADTAG=ng": 103,
            "https://i.y.qq.com/n2/m/share/details/taoge.html?ADTAG=ng&id=103": 103,
            "i.y.qq.com/n2/m/share/details/taoge.html?id=103": 103,
        }
        for link, expected in cases.items():
            with self.subTest(link=link):
                self.assertEqual(parse_playlist_id(link), expected)

    def test_parse_playlist_id_rejects_other_urls(self) -> None:
        for link in (
            "",
            "   ",
            "https://y.qq.com/n/ryqq/songDetail/004Ti8rT003TaZ",
            "https://y.qq.com/n/ryqq/albumDetail/003dYC933CfoSi",
            "https://y.qq.com/n/ryqq_v2/songDetail/004Ti8rT003TaZ",
            "https://y.qq.com/n/ryqq/playlist_v2/102",
            "https://example.com/n/ryqq/playlist/101",
            "https://example.com/n/ryqq_v2/playlist/102",
            # 白名单之外的音乐无关子域也要拒绝
            "https://news.qq.com/n/ryqq/playlist/101",
            "https://evil.qq.com/n/ryqq_v2/playlist/102",
            "https://qq.com.evil.com/n/ryqq/playlist/101",
            "https://i.y.qq.com/n2/m/share/details/taoge.html",
            "https://i.y.qq.com/n2/m/share/details/taoge.html?id=abc",
            "not a link",
        ):
            with self.subTest(link=link), self.assertRaises(PlaylistLinkError):
                parse_playlist_id(link)

    def test_parse_playlist_id_accepts_whitelisted_qq_hosts(self) -> None:
        # 白名单取自 musicdl 的 QQ_MUSIC_HOSTS。
        for host in ("y.qq.com", "i.y.qq.com", "m.y.qq.com", "c.y.qq.com", "c6.y.qq.com", "music.qq.com", "qq.com"):
            with self.subTest(host=host):
                self.assertEqual(parse_playlist_id(f"https://{host}/n/ryqq/playlist/101"), 101)

    def test_is_share_link_requires_whitelisted_host(self) -> None:
        self.assertFalse(is_share_link("https://news.qq.com/base/fcgi-bin/u?__=abc"))
        self.assertFalse(is_share_link("https://example.com/base/fcgi-bin/u?__=abc"))
        self.assertTrue(is_share_link("https://m.y.qq.com/base/fcgi-bin/u?__=abc"))

    def test_parse_playlist_id_rejects_zero(self) -> None:
        with self.assertRaises(PlaylistLinkError):
            parse_playlist_id("0")
        with self.assertRaises(PlaylistLinkError):
            parse_playlist_id("https://y.qq.com/n/ryqq/playlist/0")

    def test_is_share_link_detects_short_links(self) -> None:
        self.assertTrue(is_share_link("https://c6.y.qq.com/base/fcgi-bin/u?__=example"))
        self.assertFalse(is_share_link("https://y.qq.com/n/ryqq/playlist/101"))
        self.assertFalse(is_share_link("101"))
        self.assertFalse(is_share_link(""))

    def test_fetch_playlist_by_link_prefers_creator_musicid(self) -> None:
        detail = {
            "total_song_num": 1,
            "dirinfo": {"title": "私有", "dirid": 201, "creator": {"musicid": 900000001, "nick": "阿甲"}},
            "songlist": [{"mid": "aaa"}],
        }

        async def fake_get_detail(*args, **kwargs):  # noqa: ANN002, ANN003
            return detail

        async def run() -> None:
            service = PlaylistService(MusicService())
            try:
                with patch("qmdr.playlist.songlist.get_detail", fake_get_detail):
                    playlist = await service.fetch_playlist_by_link("101")
            finally:
                await service.music_service.close()
            self.assertEqual(playlist.owner_id, "900000001")
            # dirinfo.dirid=201 表示私有歌单，链接歌单不沿用，否则会被误判成他人私有歌单。
            self.assertEqual(playlist.dir_id, 0)

        asyncio.run(run())

    def test_fetch_playlist_by_link_requires_no_credential(self) -> None:
        detail = {
            "total_song_num": 2,
            "songlist_size": 2,
            "dirinfo": {"title": "测试歌单", "songnum": 2, "creator": {"nick": "某用户", "uin": 12345}},
            "songlist": [{"mid": "aaa"}, {"mid": "bbb"}],
        }

        async def fake_get_detail(*args, **kwargs):  # noqa: ANN002, ANN003
            return detail

        async def run() -> None:
            service = PlaylistService(MusicService())
            try:
                with patch("qmdr.playlist.songlist.get_detail", fake_get_detail):
                    playlist = await service.fetch_playlist_by_link("https://y.qq.com/n/ryqq/playlist/101")
            finally:
                await service.music_service.close()

            self.assertEqual(playlist.name, "测试歌单")
            self.assertEqual(playlist.tid, 101)
            self.assertEqual(playlist.dir_id, 0)
            self.assertEqual(playlist.song_count, 2)
            self.assertEqual(playlist.owner_name, "某用户")
            self.assertEqual(playlist.source_label, "来自 某用户")

        asyncio.run(run())

    def test_get_link_playlist_songs_hints_login_when_access_denied(self) -> None:
        async def fake_get_songlist(*args, **kwargs):  # noqa: ANN002, ANN003
            raise RuntimeError("需要登录")

        async def run() -> None:
            service = PlaylistService(MusicService())
            playlist = PlaylistItem(name="私有", dir_id=0, tid=1)
            try:
                with patch("qmdr.playlist.songlist.get_songlist", fake_get_songlist):
                    with self.assertRaises(CredentialRequiredError) as ctx:
                        await service.get_link_playlist_songs(playlist, credential=None)
            finally:
                await service.music_service.close()
            self.assertIn("登录", str(ctx.exception))

        asyncio.run(run())

    def test_get_link_playlist_songs_returns_items_without_credential(self) -> None:
        raw_songs = [
            {
                "mid": "001",
                "title": "歌名",
                "singer": [{"name": "歌手"}],
                "album": {"name": "专辑", "mid": "alb"},
                "pay": {"pay_play": 1},
            }
        ]

        async def fake_get_songlist(*args, **kwargs):  # noqa: ANN002, ANN003
            return raw_songs

        async def run() -> None:
            service = PlaylistService(MusicService())
            playlist = PlaylistItem(name="公开", dir_id=0, tid=2, song_count=1)
            try:
                with patch("qmdr.playlist.songlist.get_songlist", fake_get_songlist):
                    songs = await service.get_link_playlist_songs(playlist, credential=None)
            finally:
                await service.music_service.close()

            self.assertEqual(len(songs), 1)
            self.assertEqual(songs[0].title, "歌名")
            self.assertEqual(songs[0].singer, "歌手")
            self.assertEqual(songs[0].album_name, "专辑")
            self.assertTrue(songs[0].is_vip)

        asyncio.run(run())

    def test_song_from_raw_accepts_alternate_field_names(self) -> None:
        # 字段名兼容官方 musicu 接口（title/mid/album）与 fcg_ucc_getcdinfo 接口
        # （songname/songmid/albumname），后者是下划线风格。
        song = MusicService().song_from_raw(
            {"songmid": "002", "songname": "别名歌名", "singer": "字符串歌手", "albumname": "别名专辑"}
        )
        self.assertEqual(song.mid, "002")
        self.assertEqual(song.title, "别名歌名")
        self.assertEqual(song.singer, "字符串歌手")
        self.assertEqual(song.album_name, "别名专辑")

        named = MusicService().song_from_raw({"mid": "003", "name": "name 风格"})
        self.assertEqual(named.title, "name 风格")

    def test_coordinator_reports_empty_playlist_without_downloading(self) -> None:
        class FakeMusicService:
            async def download_song(self, *args, **kwargs):  # noqa: ANN002, ANN003
                raise AssertionError("空歌单不应触发下载")

        async def run() -> None:
            with tempfile.TemporaryDirectory() as temp:
                events: list[DownloadEvent] = []
                fake_music = FakeMusicService()
                coordinator = DownloadCoordinator(fake_music, PlaylistService(fake_music))  # type: ignore[arg-type]
                results = await coordinator.download_playlist(
                    PlaylistItem(name="空歌单", dir_id=0, tid=1),
                    "",
                    [],
                    DownloadOptions(download_dir=Path(temp)),
                    credential=None,
                    on_event=events.append,
                )
                self.assertEqual(results, [])
                self.assertEqual(events[-1].kind, "failed")
                self.assertFalse((Path(temp) / "空歌单").exists())

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
