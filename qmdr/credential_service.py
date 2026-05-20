from __future__ import annotations

import asyncio
import json
import pickle
from datetime import datetime
from pathlib import Path
from typing import Any

import aiohttp
from qqmusic_api.login import (
    Credential,
    QRCodeLoginEvents,
    QRLoginType,
    check_expired,
    check_qrcode,
    get_qrcode,
)

from .models import (
    CredentialState,
    CredentialStatus,
    QRLoginPollResult,
    QRLoginSession,
)
from .settings import legacy_credential_path, primary_credential_path
from .utils import ensure_directory


class CredentialService:
    def __init__(
        self,
        credential_path: Path | None = None,
        legacy_path: Path | None = None,
        external_api_url: str = "",
    ) -> None:
        self.credential_path = credential_path or primary_credential_path()
        self.legacy_path = legacy_path or legacy_credential_path()
        self.external_api_url = external_api_url.strip().rstrip("/")
        self.credential: Credential | None = None
        self.last_state = CredentialState(loaded=False, path=self.credential_path)

    def set_external_api_url(self, value: str) -> None:
        self.external_api_url = value.strip().rstrip("/")

    def candidate_paths(self) -> list[Path]:
        paths = [self.credential_path]
        if self.legacy_path != self.credential_path:
            paths.append(self.legacy_path)
        return paths

    def load_local_credential(self) -> tuple[Credential | None, Path | None, str]:
        first_error: tuple[Path, str] | None = None
        for path in self.candidate_paths():
            if not path.exists():
                continue
            try:
                with path.open("rb") as fp:
                    credential = pickle.load(fp)
                if isinstance(credential, Credential):
                    self.credential = credential
                    return credential, path, "已加载本地凭证"
                if first_error is None:
                    first_error = (path, "凭证文件格式不正确")
            except Exception as exc:  # noqa: BLE001 - surface a user friendly status.
                if first_error is None:
                    first_error = (path, f"加载凭证失败: {exc}")
        if first_error is not None:
            return None, first_error[0], first_error[1]
        return None, None, "未找到本地凭证"

    def save_credential(self, credential: Credential | None = None) -> Path:
        credential = credential or self.credential
        if credential is None:
            raise ValueError("没有可保存的凭证")
        ensure_directory(self.credential_path.parent)
        with self.credential_path.open("wb") as fp:
            pickle.dump(credential, fp)
        self.credential = credential
        return self.credential_path

    async def load_and_refresh_credential(self) -> CredentialState:
        credential, path, message = self.load_local_credential()
        if credential is None:
            api_credential = await self.load_from_external_api()
            if api_credential is not None:
                self.credential = api_credential
                state = CredentialState(
                    loaded=True,
                    loaded_from_api=True,
                    credential=api_credential,
                    user_id=str(api_credential.musicid or ""),
                    message="已从外部 API 加载凭证",
                )
                self.last_state = state
                return state
            state = CredentialState(loaded=False, path=path, message=message)
            self.last_state = state
            return state

        refreshed = False
        if await check_expired(credential):
            refreshed_credential = await self.refresh_credential(credential)
            if refreshed_credential is not None:
                credential = refreshed_credential
                refreshed = True
                path = self.credential_path
                message = "本地凭证已自动刷新"
            else:
                api_credential = await self.load_from_external_api()
                if api_credential is not None:
                    self.credential = api_credential
                    state = CredentialState(
                        loaded=True,
                        loaded_from_api=True,
                        credential=api_credential,
                        user_id=str(api_credential.musicid or ""),
                        message="本地凭证过期，已从外部 API 加载",
                    )
                    self.last_state = state
                    return state
                message = "本地凭证已过期且刷新失败"
                self.credential = None
                state = CredentialState(
                    loaded=False,
                    expired=True,
                    path=path,
                    user_id=str(getattr(credential, "musicid", "") or ""),
                    message=message,
                )
                self.last_state = state
                return state

        state = CredentialState(
            loaded=True,
            refreshed=refreshed,
            path=path,
            credential=credential,
            user_id=str(credential.musicid or ""),
            message=message,
        )
        self.last_state = state
        return state

    async def load_from_external_api(self) -> Credential | None:
        if not self.external_api_url:
            return None
        url = f"{self.external_api_url}/api/credential"
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as response:
                    if response.status != 200:
                        return None
                    data = await response.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return None

        cred_data = data.get("credential", {})
        if not cred_data or not cred_data.get("musicid") or not cred_data.get("musickey"):
            return None
        return Credential(
            openid=cred_data.get("openid", ""),
            refresh_token=cred_data.get("refresh_token", ""),
            access_token=cred_data.get("access_token", ""),
            expired_at=cred_data.get("expired_at", 0),
            musicid=cred_data.get("musicid", 0),
            musickey=cred_data.get("musickey", ""),
            unionid=cred_data.get("unionid", ""),
            str_musicid=cred_data.get("str_musicid", ""),
            refresh_key=cred_data.get("refresh_key", ""),
            encrypt_uin=cred_data.get("encrypt_uin", ""),
            login_type=cred_data.get("login_type", 2),
        )

    async def refresh_credential(self, credential: Credential | None = None) -> Credential | None:
        credential = credential or self.credential
        if credential is None:
            return None
        if not await credential.can_refresh():
            return None
        try:
            await credential.refresh()
            self.save_credential(credential)
            return credential
        except Exception:  # noqa: BLE001 - upstream refresh errors are not typed.
            return None

    async def check_status(self) -> CredentialStatus:
        credential, path, message = self.load_local_credential()
        if credential is None:
            return CredentialStatus(exists=False, path=path, message=message)
        try:
            expired = await check_expired(credential)
            can_refresh = await credential.can_refresh()
        except Exception as exc:  # noqa: BLE001
            return CredentialStatus(
                exists=True,
                path=path,
                user_id=str(getattr(credential, "musicid", "") or ""),
                message=f"检查凭证失败: {exc}",
            )
        return CredentialStatus(
            exists=True,
            expired=expired,
            can_refresh=can_refresh,
            path=path,
            user_id=str(credential.musicid or ""),
            message="凭证可用" if not expired else "凭证已过期",
        )

    async def start_qr_login(self, login_type: str) -> QRLoginSession:
        qr_type = QRLoginType.QQ if login_type.lower() == "qq" else QRLoginType.WX
        qr = await get_qrcode(qr_type)
        return QRLoginSession(login_type=qr_type.value, image_bytes=qr.data, qr=qr)

    async def poll_qr_login(self, session: QRLoginSession) -> QRLoginPollResult:
        event, credential = await check_qrcode(session.qr)
        if event == QRCodeLoginEvents.DONE:
            self.save_credential(credential)
            return QRLoginPollResult(
                event_name=event.name,
                done=True,
                credential=credential,
                message=f"登录成功: {credential.musicid}",
            )
        if event == QRCodeLoginEvents.TIMEOUT:
            return QRLoginPollResult(event_name=event.name, failed=True, message="二维码已过期")
        if event == QRCodeLoginEvents.REFUSE:
            return QRLoginPollResult(event_name=event.name, failed=True, message="已拒绝登录")
        if event == QRCodeLoginEvents.SCAN:
            return QRLoginPollResult(event_name=event.name, message="已扫码，请在手机上确认")
        if event == QRCodeLoginEvents.CONF:
            return QRLoginPollResult(event_name=event.name, message="等待确认登录")
        return QRLoginPollResult(event_name=event.name, message=f"二维码状态: {event.name}")

    def _credential_export_data(self, credential: Credential) -> dict[str, Any]:
        export_data: dict[str, Any] = {}
        for key, value in credential.__dict__.items():
            if isinstance(value, (str, int, float, bool, type(None))):
                export_data[key] = value
            else:
                export_data[key] = str(value)
        return export_data

    def export_credential_to_json_file(self, output_path: Path) -> Path:
        credential, _, _ = self.load_local_credential()
        if credential is None:
            raise ValueError("未找到凭证，无法导出")

        output_path = output_path.expanduser()
        if output_path.suffix.lower() != ".json":
            output_path = output_path.with_suffix(".json")
        ensure_directory(output_path.parent)
        with output_path.open("w", encoding="utf-8") as fp:
            json.dump(self._credential_export_data(credential), fp, ensure_ascii=False, indent=2)
        return output_path

    def export_credential_to_json(self, output_dir: Path | None = None) -> Path:
        credential, _, _ = self.load_local_credential()
        if credential is None:
            raise ValueError("未找到凭证，无法导出")
        output_dir = ensure_directory(output_dir or Path.cwd())
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        json_file = output_dir / f"qqmusic_credential_{timestamp}.json"
        with json_file.open("w", encoding="utf-8") as fp:
            json.dump(self._credential_export_data(credential), fp, ensure_ascii=False, indent=2)
        return json_file
