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
from qmdr.playlist import PlaylistService
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

    def test_playlist_folder_uses_unique_name_but_keeps_existing_legacy_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            playlist = PlaylistItem(name="Daily", dir_id=123, tid=456)
            service = PlaylistService(MusicService())

            self.assertEqual(service.playlist_folder(root, playlist, "user"), root / "user_123_Daily")

            legacy = root / "Daily"
            legacy.mkdir()
            self.assertEqual(service.playlist_folder(root, playlist, "user"), legacy)


if __name__ == "__main__":
    unittest.main()
