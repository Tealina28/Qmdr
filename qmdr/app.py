from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from pathlib import Path

import flet as ft

from .models import DownloadEvent, DownloadOptions, PlaylistItem, SongItem
from .quality import QUALITY_OPTIONS
from .settings import default_download_dir, load_download_dir, save_download_dir
from .utils import clamp_int


APP_ROOT = Path(__file__).resolve().parent.parent
ASSETS_DIR = APP_ROOT / "assets"
APP_ICON_PNG = ASSETS_DIR / "qmdr.png"
APP_ICON_ICO = ASSETS_DIR / "qmdr.ico"


def _border(color: str = "#d8dee9") -> ft.Border:
    side = ft.BorderSide(1, color)
    return ft.Border(side, side, side, side)


def _section(title: str, controls: list[ft.Control], expand: bool | int | None = None) -> ft.Container:
    return ft.Container(
        expand=expand,
        padding=14,
        border=_border(),
        border_radius=6,
        bgcolor="#ffffff",
        content=ft.Column(
            spacing=12,
            controls=[
                ft.Text(title, size=15, weight=ft.FontWeight.BOLD),
                *controls,
            ],
        ),
    )


NAV_ITEMS = [
    (ft.Icons.SEARCH, "搜索下载"),
    (ft.Icons.LIBRARY_MUSIC, "歌单下载"),
    (ft.Icons.QUEUE_MUSIC, "下载队列"),
    (ft.Icons.SETTINGS, "凭证设置"),
]


class QmdrApp:
    def __init__(self, page: ft.Page) -> None:
        self.page = page
        self.credential_service = None
        self.music_service = None
        self.playlist_service = None
        self.coordinator = None
        self.credential_required_error_type: type[Exception] | None = None
        self.is_share_link: Callable[[str], bool] | None = None

        self.credential = None
        self.search_songs: list[SongItem] = []
        self.playlists: list[PlaylistItem] = []
        self.playlist_songs: list[SongItem] = []
        self.songs_playlist: PlaylistItem | None = None
        self.selected_playlist: PlaylistItem | None = None
        self.selected_playlist_from_link = False
        self.active_download = False
        self.qr_cancelled = False
        self.search_request_id = 0
        self.playlist_request_id = 0
        self.preview_request_id = 0
        self.link_request_id = 0
        self.selected_nav_index = 0
        self.nav_collapsed = False
        self.app_icon_bytes = self.load_app_icon_bytes()

        self.quality_value = "3"
        self.search_quality_dropdown = self.make_quality_dropdown()
        self.playlist_quality_dropdown = self.make_quality_dropdown()
        self.settings_quality_dropdown = self.make_quality_dropdown()
        self.download_dir_input = ft.TextField(
            label="下载目录",
            value=str(load_download_dir()),
            expand=True,
            on_blur=self.on_download_dir_commit,
            on_submit=self.on_download_dir_commit,
        )
        self.batch_size_input = ft.TextField(label="并发", value="5", width=92)
        self.cover_size_dropdown = ft.Dropdown(
            label="封面",
            value="800",
            width=120,
            options=[ft.DropdownOption(key=str(size), text=f"{size}px") for size in (150, 300, 500, 800)],
        )
        self.overwrite_checkbox = ft.Checkbox(label="覆盖已存在文件", value=False)
        self.external_api_input = ft.TextField(label="外部凭证 API（可选）", value="", expand=True)

        self.credential_text = ft.Text("正在检查凭证...", size=13)
        self.search_input = ft.TextField(label="歌曲关键词", expand=True)
        self.search_status = ft.Text("", size=13, color="#52616b")
        self.search_results_list = ft.ListView(expand=True, spacing=8, padding=0)

        self.musicid_input = ft.TextField(label="musicid(通常为 qq 号或微信 id)", expand=True)
        self.playlist_status = ft.Text("", size=13, color="#52616b")
        self.playlist_list = ft.ListView(expand=True, spacing=8, padding=0)
        self.playlist_preview = ft.ListView(expand=True, spacing=4, padding=0)

        self.link_input = ft.TextField(label="歌单链接或歌单 ID", expand=True)
        self.link_status = ft.Text("", size=13, color="#52616b")

        self.current_task_text = ft.Text("暂无任务", size=14, weight=ft.FontWeight.BOLD)
        self.progress_bar = ft.ProgressBar(value=0, bar_height=8, height=8, border_radius=8, track_gap=0)
        self.progress_text = ft.Text("0/0", size=13, color="#52616b")
        self.download_log = ft.ListView(expand=True, spacing=6, padding=0)
        self.file_picker = ft.FilePicker()

        self.views: list[ft.Control] = []
        self.nav = self.build_nav()

    async def start(self) -> None:
        self.page.title = "Qmdr"
        self.page.bgcolor = "#f3f6fa"
        self.page.padding = 0
        self.page.theme_mode = ft.ThemeMode.LIGHT
        if APP_ICON_ICO.exists():
            self.page.window.icon = str(APP_ICON_ICO)

        self.search_input.on_submit = self.on_search
        self.musicid_input.on_submit = self.on_load_playlists
        self.link_input.on_submit = self.on_load_link_playlist
        self.page.services.append(self.file_picker)

        self.views = [
            self.build_search_view(),
            self.build_playlist_view(),
            self.build_queue_view(),
            self.build_settings_view(),
        ]
        for index, view in enumerate(self.views):
            view.visible = index == 0

        self.page.add(
            ft.SafeArea(
                expand=True,
                content=ft.Row(
                    expand=True,
                    spacing=0,
                    controls=[
                        self.nav,
                        ft.VerticalDivider(width=1),
                        ft.Container(
                            expand=True,
                            padding=18,
                            content=ft.Column(expand=True, controls=self.views),
                        ),
                    ],
                ),
            )
        )
        self.page.update()
        self.page.run_task(self.initialize_services)

    async def initialize_services(self) -> None:
        try:
            self.ensure_services()
            await self.reload_credential(show_message=False)
        except Exception as exc:  # noqa: BLE001
            self.credential_text.value = "初始化失败，请重启应用或检查依赖"
            self.page.update()
            return

    def ensure_services(self) -> None:
        if self.credential_service is not None:
            return
        from .coordinator import DownloadCoordinator
        from .credential_service import CredentialService
        from .music import MusicService
        from .playlist import CredentialRequiredError, PlaylistService, is_share_link

        self.credential_service = CredentialService()
        self.music_service = MusicService()
        self.playlist_service = PlaylistService(self.music_service)
        self.coordinator = DownloadCoordinator(self.music_service, self.playlist_service)
        self.credential_required_error_type = CredentialRequiredError
        self.is_share_link = is_share_link

    def is_credential_required_error(self, exc: Exception) -> bool:
        return self.credential_required_error_type is not None and isinstance(exc, self.credential_required_error_type)

    def load_app_icon_bytes(self) -> bytes | None:
        try:
            return APP_ICON_PNG.read_bytes()
        except OSError:
            return None

    def app_icon(self, size: int = 22) -> ft.Control:
        if self.app_icon_bytes is None:
            return ft.Icon(ft.Icons.MUSIC_NOTE, size=size, color="#263241")
        return ft.Image(src=self.app_icon_bytes, width=size, height=size, fit=ft.BoxFit.CONTAIN)

    def build_nav(self) -> ft.Container:
        return ft.Container(
            width=208,
            bgcolor="#f7f9fc",
            padding=ft.Padding.symmetric(horizontal=12, vertical=14),
            content=self.build_nav_content(),
        )

    def build_nav_content(self) -> ft.Column:
        toggle_button = ft.IconButton(
            icon=ft.Icons.CHEVRON_RIGHT if self.nav_collapsed else ft.Icons.CHEVRON_LEFT,
            tooltip="展开导航" if self.nav_collapsed else "折叠导航",
            on_click=self.on_toggle_nav,
        )
        header = (
            ft.Container(width=64, height=40, alignment=ft.Alignment.CENTER, content=self.app_icon(26))
            if self.nav_collapsed
            else ft.Container(
                width=176,
                height=40,
                padding=ft.Padding.symmetric(horizontal=16),
                content=ft.Row(
                    spacing=8,
                    alignment=ft.MainAxisAlignment.START,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        self.app_icon(22),
                        ft.Text("Qmdr", size=16, weight=ft.FontWeight.BOLD, color="#1f2937"),
                    ],
                ),
            )
        )
        footer = ft.Container(
            width=64 if self.nav_collapsed else 176,
            height=48,
            alignment=ft.Alignment.CENTER if self.nav_collapsed else ft.Alignment.CENTER_RIGHT,
            content=toggle_button,
        )
        return ft.Column(
            expand=True,
            spacing=6,
            controls=[
                header,
                ft.Container(height=4),
                *[self.build_nav_item(index, icon, label) for index, (icon, label) in enumerate(NAV_ITEMS)],
                ft.Container(expand=True),
                footer,
            ],
        )

    def build_nav_item(self, index: int, icon: ft.IconData, label: str) -> ft.Container:
        selected = index == self.selected_nav_index
        color = "#263241" if selected else "#4b5563"
        controls: list[ft.Control] = [ft.Icon(icon, size=25, color=color)]
        if not self.nav_collapsed:
            controls.append(
                ft.Text(
                    label,
                    size=14,
                    weight=ft.FontWeight.BOLD if selected else ft.FontWeight.W_500,
                    color=color,
                )
            )
        return ft.Container(
            width=64 if self.nav_collapsed else 176,
            height=50,
            border_radius=25,
            bgcolor="#dbe8fb" if selected else None,
            padding=ft.Padding.symmetric(horizontal=0 if self.nav_collapsed else 16),
            ink=True,
            ink_color="#d9e7fb",
            on_click=lambda e, item_index=index: self.select_nav(item_index),
            content=ft.Row(
                spacing=14,
                alignment=ft.MainAxisAlignment.CENTER if self.nav_collapsed else ft.MainAxisAlignment.START,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=controls,
            ),
        )

    def refresh_nav(self) -> None:
        self.nav.width = 88 if self.nav_collapsed else 208
        self.nav.padding = ft.Padding.symmetric(horizontal=12, vertical=14)
        self.nav.content = self.build_nav_content()

    def select_nav(self, index: int) -> None:
        self.selected_nav_index = index
        for view_index, view in enumerate(self.views):
            view.visible = view_index == index
        self.refresh_nav()
        self.page.update()

    def on_toggle_nav(self, event: ft.Event | None = None) -> None:
        self.nav_collapsed = not self.nav_collapsed
        self.refresh_nav()
        self.page.update()

    def build_search_view(self) -> ft.Container:
        return ft.Container(
            expand=True,
            content=ft.Column(
                expand=True,
                spacing=14,
                controls=[
                    _section(
                        "搜索单曲",
                        [
                            ft.Row(
                                controls=[
                                    self.search_input,
                                    ft.Button("搜索", icon=ft.Icons.SEARCH, on_click=self.on_search),
                                ]
                            ),
                            ft.Row(
                                wrap=True,
                                spacing=10,
                                controls=[
                                    self.search_quality_dropdown,
                                ],
                            ),
                            self.search_status,
                        ],
                    ),
                    _section("搜索结果", [self.search_results_list], expand=True),
                ],
            ),
        )

    def build_playlist_view(self) -> ft.Container:
        return ft.Container(
            expand=True,
            content=ft.Column(
                expand=True,
                spacing=14,
                controls=[
                    _section(
                        "下载音质（对账号歌单和歌单链接均生效）",
                        [self.playlist_quality_dropdown],
                    ),
                    _section(
                        "歌单链接解析（公开歌单无需登录）",
                        [
                            ft.Row(
                                controls=[
                                    self.link_input,
                                    ft.Button("解析歌单", icon=ft.Icons.LINK, on_click=self.on_load_link_playlist),
                                    ft.Button("下载歌单", icon=ft.Icons.DOWNLOAD, on_click=self.on_download_link_playlist),
                                ]
                            ),
                            ft.Text(
                                "支持 y.qq.com/n/ryqq/playlist/<ID>、y.qq.com/n/ryqq_v2/playlist/<ID>、"
                                "i.y.qq.com/n2/m/share/details/taoge.html?id=<ID>，或直接粘贴歌单 ID。",
                                size=12,
                                color="#8792a2",
                            ),
                            self.link_status,
                        ],
                    ),
                    _section(
                        "账号歌单解析",
                        [
                            ft.Row(
                                controls=[
                                    self.musicid_input,
                                    ft.Button("获取歌单", icon=ft.Icons.PLAYLIST_PLAY, on_click=self.on_load_playlists),
                                    ft.Button("下载全部", icon=ft.Icons.DOWNLOAD, on_click=self.on_download_all_playlists),
                                ]
                            ),
                            self.playlist_status,
                        ],
                    ),
                    ft.Row(
                        expand=True,
                        spacing=14,
                        controls=[
                            _section("歌单列表", [self.playlist_list], expand=1),
                            _section("歌曲预览", [self.playlist_preview], expand=2),
                        ],
                    ),
                ],
            ),
        )

    def build_queue_view(self) -> ft.Container:
        return ft.Container(
            expand=True,
            content=ft.Column(
                expand=True,
                spacing=14,
                controls=[
                    _section(
                        "当前任务",
                        [
                            self.current_task_text,
                            self.progress_bar,
                            ft.Row(
                                controls=[
                                    self.progress_text,
                                    ft.Button("取消", icon=ft.Icons.CANCEL, on_click=self.on_cancel_download),
                                    ft.Button("打开目录", icon=ft.Icons.FOLDER_OPEN, on_click=self.on_open_download_dir),
                                ]
                            ),
                        ],
                    ),
                    _section("日志", [self.download_log], expand=True),
                ],
            ),
        )

    def build_settings_view(self) -> ft.Container:
        return ft.Container(
            expand=True,
            content=ft.Column(
                expand=True,
                spacing=14,
                controls=[
                    _section(
                        "凭证",
                        [
                            self.credential_text,
                            ft.Row(
                                wrap=True,
                                spacing=10,
                                controls=[
                                    ft.Button("QQ 登录", icon=ft.Icons.LOGIN, on_click=lambda e: self.page.run_task(self.on_qr_login, "qq")),
                                    ft.Button("微信登录", icon=ft.Icons.LOGIN, on_click=lambda e: self.page.run_task(self.on_qr_login, "wx")),
                                    ft.Button("刷新凭证", icon=ft.Icons.REFRESH, on_click=self.on_refresh_credential),
                                    ft.Button("导出凭证 JSON", icon=ft.Icons.SAVE, on_click=self.on_export_credential),
                                ],
                            ),
                        ],
                    ),
                    _section(
                        "下载设置",
                        [
                            ft.Row(
                                controls=[
                                    self.download_dir_input,
                                    ft.Button("选择", icon=ft.Icons.FOLDER_OPEN, on_click=self.on_pick_download_dir),
                                ]
                            ),
                            ft.Row(
                                wrap=True,
                                spacing=10,
                                controls=[
                                    self.settings_quality_dropdown,
                                    self.cover_size_dropdown,
                                    self.batch_size_input,
                                    self.overwrite_checkbox,
                                ],
                            ),
                        ],
                    ),
                    _section(
                        "高级",
                        [
                            ft.Row(
                                controls=[
                                    self.external_api_input,
                                    ft.Button("重新加载凭证", icon=ft.Icons.KEY, on_click=self.on_reload_with_external_api),
                                ]
                            ),
                        ],
                    ),
                ],
            ),
        )

    def make_quality_dropdown(self) -> ft.Dropdown:
        return ft.Dropdown(
            label="音质",
            value=self.quality_value,
            width=280,
            options=[ft.DropdownOption(key=str(key), text=name) for key, (name, _) in QUALITY_OPTIONS.items()],
            on_select=self.on_quality_select,
        )

    def on_quality_select(self, event: ft.Event[ft.Dropdown]) -> None:
        self.quality_value = event.control.value or "3"
        for control in (
            self.search_quality_dropdown,
            self.playlist_quality_dropdown,
            self.settings_quality_dropdown,
        ):
            if control is not event.control:
                control.value = self.quality_value
        self.page.update()

    def options(self) -> DownloadOptions:
        self.save_download_dir_setting()
        return DownloadOptions(
            download_dir=Path(self.download_dir_input.value or default_download_dir()),
            quality_level=clamp_int(self.quality_value, 3, 1, 4),
            cover_size=clamp_int(self.cover_size_dropdown.value, 800, 150, 800),
            batch_size=clamp_int(self.batch_size_input.value, 5, 1, 12),
            overwrite=bool(self.overwrite_checkbox.value),
        )

    def save_download_dir_setting(self, show_message: bool = False) -> None:
        value = (self.download_dir_input.value or "").strip()
        if not value:
            return
        try:
            save_download_dir(Path(value))
        except OSError as exc:
            self.toast(f"保存下载目录失败: {exc}")
            return
        if show_message:
            self.toast("下载目录已保存")

    def on_download_dir_commit(self, event: ft.Event[ft.TextField] | None = None) -> None:
        self.save_download_dir_setting()

    async def reload_credential(self, show_message: bool = True) -> None:
        self.ensure_services()
        self.credential_service.set_external_api_url(self.external_api_input.value or "")
        state = await self.credential_service.load_and_refresh_credential()
        self.credential = state.credential if state.loaded else None
        if state.loaded:
            source = "外部 API" if state.loaded_from_api else "本地"
            refreshed = "，已刷新" if state.refreshed else ""
            self.credential_text.value = f"已登录: {state.user_id or '未知用户'}（{source}{refreshed}）"
        else:
            self.credential_text.value = f"未登录: {state.message}"
        if show_message:
            self.toast(self.credential_text.value)
        self.page.update()

    async def on_search(self, event: ft.Event | None = None) -> None:
        self.ensure_services()
        keyword = (self.search_input.value or "").strip()
        if not keyword:
            self.toast("请输入歌曲关键词")
            return
        self.search_request_id += 1
        request_id = self.search_request_id
        self.search_status.value = "搜索中..."
        self.search_results_list.controls.clear()
        self.page.update()
        try:
            songs = await self.music_service.search_songs(keyword)
        except Exception as exc:  # noqa: BLE001
            if request_id != self.search_request_id:
                return
            self.search_status.value = f"搜索失败: {exc}"
            self.page.update()
            return
        if request_id != self.search_request_id:
            return
        self.search_songs = songs
        self.search_status.value = f"找到 {len(self.search_songs)} 个结果"
        self.render_search_results()

    def render_search_results(self) -> None:
        self.search_results_list.controls.clear()
        for index, song in enumerate(self.search_songs, 1):
            vip = "  VIP" if song.is_vip else ""
            self.search_results_list.controls.append(
                ft.Container(
                    padding=10,
                    border=_border("#e1e7ef"),
                    border_radius=6,
                    bgcolor="#fbfcfe",
                    content=ft.Row(
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        controls=[
                            ft.Column(
                                expand=True,
                                spacing=3,
                                controls=[
                                    ft.Text(f"{index}. {song.title}{vip}", weight=ft.FontWeight.BOLD),
                                    ft.Text(song.singer, size=12, color="#52616b"),
                                    ft.Text(song.album_name or "未知专辑", size=12, color="#8792a2"),
                                ],
                            ),
                            ft.Button("下载", icon=ft.Icons.DOWNLOAD, on_click=lambda e, item=song: self.start_song_download(item)),
                        ],
                    ),
                )
            )
        self.page.update()

    def start_song_download(self, song: SongItem) -> None:
        if self.active_download:
            self.toast("已有下载任务在运行")
            return
        self.page.run_task(self.run_single_download, song)

    async def run_single_download(self, song: SongItem) -> None:
        self.ensure_services()
        self.set_download_running(True)
        self.switch_to_queue()
        try:
            await self.coordinator.download_songs([song], self.options(), self.credential, self.on_download_event)
        finally:
            self.set_download_running(False)

    async def on_load_playlists(self, event: ft.Event | None = None) -> None:
        self.ensure_services()
        user_id = (self.musicid_input.value or "").strip()
        if not user_id:
            self.toast("请输入 musicid")
            return
        self.playlist_request_id += 1
        request_id = self.playlist_request_id
        self.playlist_status.value = "正在获取歌单..."
        self.playlist_list.controls.clear()
        self.playlist_preview.controls.clear()
        self.page.update()
        try:
            playlists = await self.playlist_service.get_user_playlists(user_id, self.credential)
        except Exception as exc:  # noqa: BLE001
            if request_id != self.playlist_request_id:
                return
            if self.is_credential_required_error(exc):
                self.playlist_status.value = str(exc)
                self.page.update()
                return
            self.playlist_status.value = f"获取歌单失败: {exc}"
            self.page.update()
            return
        if request_id != self.playlist_request_id:
            return
        self.playlists = playlists
        self.playlist_status.value = f"找到 {len(self.playlists)} 个歌单"
        self.render_playlists()

    def render_playlists(self) -> None:
        self.playlist_list.controls.clear()
        for index, playlist in enumerate(self.playlists, 1):
            self.playlist_list.controls.append(
                ft.Container(
                    padding=10,
                    border=_border("#e1e7ef"),
                    border_radius=6,
                    bgcolor="#fbfcfe",
                    content=ft.Row(
                        controls=[
                            ft.Column(
                                expand=True,
                                spacing=3,
                                controls=[
                                    ft.Text(f"{index}. {playlist.name}", weight=ft.FontWeight.BOLD),
                                    ft.Text(f"{playlist.song_count} 首", size=12, color="#52616b"),
                                ],
                            ),
                            ft.IconButton(ft.Icons.OPEN_IN_NEW, tooltip="预览", on_click=lambda e, item=playlist: self.page.run_task(self.preview_playlist, item)),
                            ft.IconButton(ft.Icons.DOWNLOAD, tooltip="下载", on_click=lambda e, item=playlist: self.page.run_task(self.download_playlist, item)),
                        ],
                    ),
                )
            )
        self.page.update()

    async def preview_playlist(self, playlist: PlaylistItem) -> None:
        self.ensure_services()
        self.selected_playlist = playlist
        self.selected_playlist_from_link = False
        self.preview_request_id += 1
        request_id = self.preview_request_id
        self.playlist_preview.controls = [ft.Text("正在加载歌曲...")]
        self.page.update()
        try:
            songs = await self.fetch_playlist_songs(playlist)
        except Exception as exc:  # noqa: BLE001
            if request_id != self.preview_request_id:
                return
            self.playlist_preview.controls = [ft.Text(f"预览失败: {exc}", color="#b42318")]
            self.page.update()
            return
        if request_id != self.preview_request_id:
            return
        self.playlist_songs = songs
        self.songs_playlist = playlist
        self.render_playlist_preview(playlist)

    def render_playlist_preview(self, playlist: PlaylistItem) -> None:
        from_link = self.selected_playlist_from_link
        self.playlist_preview.controls.clear()
        self.playlist_preview.controls.append(
            ft.Row(
                controls=[
                    ft.Text(f"{playlist.name}：{len(self.playlist_songs)} 首", weight=ft.FontWeight.BOLD, expand=True),
                    ft.Button(
                        "下载此歌单",
                        icon=ft.Icons.DOWNLOAD,
                        on_click=lambda e, item=playlist: self.page.run_task(
                            self.on_download_link_playlist if from_link else self.download_playlist,
                            item,
                        ),
                    ),
                ]
            )
        )
        for index, song in enumerate(self.playlist_songs, 1):
            vip = "  VIP" if song.is_vip else ""
            self.playlist_preview.controls.append(ft.Text(f"{index}. {song.singer} - {song.title}{vip}", size=13))
        self.page.update()

    async def on_load_link_playlist(self, event: ft.Event | None = None) -> None:
        self.ensure_services()
        link = (self.link_input.value or "").strip()
        if not link:
            self.toast("请输入歌单链接或歌单 ID")
            return
        self.link_request_id += 1
        request_id = self.link_request_id
        self.link_status.value = "正在解析歌单..."
        self.playlist_preview.controls.clear()
        self.page.update()
        try:
            if self.is_share_link is not None and self.is_share_link(link):
                self.link_status.value = "正在解析分享短链..."
                self.page.update()
                link = await self.playlist_service.resolve_share_link(link)
            playlist = await self.playlist_service.fetch_playlist_by_link(link)
            songs = await self.playlist_service.get_link_playlist_songs(playlist, self.credential)
        except Exception as exc:  # noqa: BLE001
            if request_id != self.link_request_id:
                return
            self.selected_playlist_from_link = False
            self.link_status.value = f"解析失败: {exc}"
            self.page.update()
            return
        if request_id != self.link_request_id:
            return
        self.selected_playlist = playlist
        self.selected_playlist_from_link = True
        self.playlist_songs = songs
        self.songs_playlist = playlist
        self.link_status.value = f"{playlist.name}（{playlist.source_label}）：{len(songs)} 首"
        self.render_playlist_preview(playlist)

    async def on_download_link_playlist(self, playlist: PlaylistItem) -> None:
        self.ensure_services()
        await self.download_playlist(playlist, from_link=True)

    async def fetch_playlist_songs(self, playlist: PlaylistItem) -> list[SongItem]:
        if self.selected_playlist_from_link:
            return await self.playlist_service.get_link_playlist_songs(playlist, self.credential)
        user_id = (self.musicid_input.value or "").strip()
        if not user_id:
            raise ValueError("请输入 musicid")
        return await self.playlist_service.get_playlist_songs(playlist, user_id, self.credential)

    async def download_playlist(self, playlist: PlaylistItem, from_link: bool = False) -> None:
        self.ensure_services()
        if self.active_download:
            self.toast("已有下载任务在运行")
            return
        # 以调用方传入的来源为准，避免上一次预览链接歌单后残留的状态串到本次下载。
        self.selected_playlist = playlist
        self.selected_playlist_from_link = from_link
        user_id = "" if from_link else (self.musicid_input.value or "").strip()
        if not from_link and not user_id:
            self.toast("请输入 musicid")
            return
        self.set_download_running(True)
        self.switch_to_queue()
        try:
            songs = self.playlist_songs if self.songs_playlist == playlist else []
            if not songs:
                songs = await self.fetch_playlist_songs(playlist)
            await self.coordinator.download_playlist(playlist, user_id, songs, self.options(), self.credential, self.on_download_event)
        except Exception as exc:  # noqa: BLE001
            await self.on_download_event(DownloadEvent(kind="failed", message=f"歌单下载失败: {exc}", error=str(exc)))
        finally:
            self.set_download_running(False)

    async def on_download_all_playlists(self, event: ft.Event | None = None) -> None:
        self.ensure_services()
        if self.active_download:
            self.toast("已有下载任务在运行")
            return
        user_id = (self.musicid_input.value or "").strip()
        if not user_id:
            self.toast("请输入 musicid")
            return
        if not self.playlists:
            await self.on_load_playlists()
        if not self.playlists:
            return
        self.set_download_running(True)
        self.switch_to_queue()
        self.coordinator.reset()
        cancelled_before_playlist = False
        try:
            for playlist in self.playlists:
                if self.coordinator.cancel_requested:
                    cancelled_before_playlist = True
                    break
                try:
                    songs = await self.playlist_service.get_playlist_songs(playlist, user_id, self.credential)
                    if self.coordinator.cancel_requested:
                        cancelled_before_playlist = True
                        break
                    await self.coordinator.download_playlist(
                        playlist,
                        user_id,
                        songs,
                        self.options(),
                        self.credential,
                        self.on_download_event,
                        reset_cancel=False,
                    )
                except Exception as exc:  # noqa: BLE001
                    await self.on_download_event(DownloadEvent(kind="failed", message=f"{playlist.name}: {exc}", error=str(exc)))
            if cancelled_before_playlist:
                await self.on_download_event(DownloadEvent(kind="cancelled", message="下载已取消"))
        finally:
            self.set_download_running(False)

    async def on_download_event(self, event: DownloadEvent) -> None:
        if event.kind in {"start", "playlist", "progress", "done", "cancelled"} and event.total:
            self.progress_bar.value = min(1, max(0, event.current / event.total))
            self.progress_text.value = f"{event.current}/{event.total}"
        if event.kind in {"start", "playlist", "downloading", "file_progress", "progress", "done", "cancelled"}:
            self.current_task_text.value = event.message
        if event.kind == "file_progress":
            self.page.update()
            return
        color = {
            "success": "#087443",
            "failed": "#b42318",
            "warning": "#b54708",
            "skipped": "#52616b",
            "fallback": "#52616b",
        }.get(event.kind, "#1f2937")
        self.download_log.controls.append(ft.Text(event.message, size=12, color=color))
        if len(self.download_log.controls) > 220:
            self.download_log.controls = self.download_log.controls[-220:]
        self.page.update()

    def set_download_running(self, running: bool) -> None:
        self.active_download = running
        if running:
            self.progress_bar.value = 0
            self.progress_text.value = "0/0"
        self.page.update()

    def switch_to_queue(self) -> None:
        self.select_nav(2)

    def on_cancel_download(self, event: ft.Event | None = None) -> None:
        self.ensure_services()
        self.coordinator.cancel()
        self.toast("已请求取消，当前批次结束后停止")

    async def on_pick_download_dir(self, event: ft.Event | None = None) -> None:
        try:
            selected = await self.file_picker.get_directory_path(dialog_title="选择下载目录")
        except Exception as exc:  # noqa: BLE001
            self.toast(f"无法打开目录选择器: {exc}")
            return
        if selected:
            self.download_dir_input.value = selected
            self.save_download_dir_setting(show_message=True)
            self.page.update()

    def on_open_download_dir(self, event: ft.Event | None = None) -> None:
        path = Path(self.download_dir_input.value or default_download_dir()).resolve()
        path.mkdir(parents=True, exist_ok=True)
        try:
            if os.name == "nt":
                os.startfile(path)  # type: ignore[attr-defined]
            else:
                self.page.launch_url(path.as_uri())
        except Exception as exc:  # noqa: BLE001
            self.toast(f"打开目录失败: {exc}")

    async def on_refresh_credential(self, event: ft.Event | None = None) -> None:
        self.ensure_services()
        refreshed = await self.credential_service.refresh_credential()
        if refreshed is None:
            self.toast("凭证刷新失败或不可刷新")
        await self.reload_credential(show_message=True)

    async def on_reload_with_external_api(self, event: ft.Event | None = None) -> None:
        await self.reload_credential(show_message=True)

    async def on_export_credential(self, event: ft.Event | None = None) -> None:
        self.ensure_services()
        default_name = "qqmusic_credential.json"
        try:
            selected = await self.file_picker.save_file(
                dialog_title="导出凭证 JSON",
                file_name=default_name,
                initial_directory=str(APP_ROOT),
                file_type=ft.FilePickerFileType.CUSTOM,
                allowed_extensions=["json"],
            )
        except Exception as exc:  # noqa: BLE001
            self.toast(f"无法打开保存对话框: {exc}")
            return
        if not selected:
            return
        try:
            path = self.credential_service.export_credential_to_json_file(Path(selected))
        except Exception as exc:  # noqa: BLE001
            self.toast(str(exc))
            return
        self.toast(f"已导出凭证 JSON: {path}")

    async def on_qr_login(self, login_type: str) -> None:
        self.ensure_services()
        self.qr_cancelled = False
        status = ft.Text("正在获取二维码...", size=13)
        dialog = ft.AlertDialog(
            modal=True,
            title=f"{'QQ' if login_type == 'qq' else '微信'}扫码登录",
            content=ft.Column(
                tight=True,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Container(width=240, height=240, content=ft.ProgressBar()),
                status,
            ],
            ),
            actions=[ft.Button("取消", icon=ft.Icons.CANCEL, on_click=lambda e: self.cancel_qr_dialog())],
        )
        self.page.show_dialog(dialog)
        self.page.update()

        try:
            session = await self.credential_service.start_qr_login(login_type)
        except Exception as exc:  # noqa: BLE001
            status.value = f"获取二维码失败: {exc}"
            self.page.update()
            return

        dialog.content = ft.Column(
            tight=True,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Image(src=session.image_bytes, width=240, height=240, fit=ft.BoxFit.CONTAIN),
                status,
            ],
        )
        status.value = "请使用手机扫码"
        self.page.update()

        while not self.qr_cancelled:
            try:
                result = await self.credential_service.poll_qr_login(session)
            except Exception as exc:  # noqa: BLE001
                status.value = f"登录检查失败: {exc}"
                self.page.update()
                return
            status.value = result.message or f"状态: {result.event_name}"
            self.page.update()
            if result.done:
                self.page.pop_dialog()
                await self.reload_credential(show_message=True)
                return
            if result.failed:
                return
            await asyncio.sleep(2)

    def cancel_qr_dialog(self) -> None:
        self.qr_cancelled = True
        self.page.pop_dialog()
        self.page.update()

    def toast(self, message: str) -> None:
        self.page.show_dialog(ft.SnackBar(ft.Text(message), show_close_icon=True))
        self.page.update()


async def main(page: ft.Page) -> None:
    app = QmdrApp(page)
    await app.start()
