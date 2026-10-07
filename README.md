# Qmdr

Qmdr 是一个基于 Flet 的 QQ 音乐桌面下载工具，支持本地下载 QQ 音乐单曲和歌单。

本项目基于 GPL-3.0 项目 [qq-music-download](https://github.com/tooplick/qq-music-download) 实现图形界面。

## 运行

```powershell
uv run flet run -a assets main.py
```

## 热重载

```powershell
uv run flet run --recursive --ignore-dirs .venv,.agents,__pycache__ -a assets main.py
```

`--recursive` 会递归监听项目文件变化。建议忽略 `.venv`、`.agents` 和
`__pycache__`，避免依赖或缓存文件变化触发重复重载，也能减少启动和监听开销。

## 功能

- QQ / 微信扫码登录，并将凭证保存到本地。
- 单曲搜索下载，支持音质自动降级。
- 歌单预览和批量下载。
- 歌单链接直接下载：粘贴歌单链接或歌单 ID 即可解析并下载，公开歌单无需登录。
- 为下载文件写入封面、歌词和基础元数据。
- 可选保存独立歌词文件（`.lrc`），与音频同目录同名。
- 本地下载队列，显示进度和单曲状态。

## 歌单链接下载

在「歌单下载」页的「歌单链接解析」区域粘贴链接，点「解析歌单」预览、点「下载歌单」开始下载。
顶部的「下载音质」独立成一区，对「歌单链接解析」和「账号歌单解析」都生效。
下载会保存到下载目录下以歌单名命名的子文件夹。

支持的链接格式（`<歌单ID>` 替换为实际数字歌单 ID）：

- `https://y.qq.com/n/ryqq/playlist/<歌单ID>`
- `https://y.qq.com/n/ryqq_v2/playlist/<歌单ID>`（新版分享链接，`ADTAG` 等查询参数会被忽略）
- `https://i.y.qq.com/n2/m/share/details/taoge.html?id=<歌单ID>`
- QQ 音乐分享短链 `https://c6.y.qq.com/base/fcgi-bin/u?__=xxxx`（自动跟随跳转）
- 直接粘贴纯数字歌单 ID（即 `<歌单ID>` 本身），无需链接。

公开歌单无需登录即可解析和下载；私有歌单或他人「我喜欢」会提示先到「设置」登录后重试。

域名按 QQ 音乐官方白名单校验（`y.qq.com`、`i.y.qq.com`、`m.y.qq.com`、`c.y.qq.com`、`c6.y.qq.com`、
`music.qq.com`、`qq.com`），白名单之外的链接会被拒绝——如果遇到未收录的域名，直接粘贴纯数字歌单 ID 即可。

链接格式参考了 [QMDown](https://github.com/ouomj/QMDown) 的 `extractor/songlist.py`，并额外兼容新版
`ryqq_v2` 路径、分享短链和纯数字歌单 ID。域名白名单取自
[musicdl](https://github.com/CharlesPikachu/musicdl)（PolyForm Noncommercial License，仅参考思路，
未复制其代码；本项目仍为 GPL-3.0）。

## 歌词文件

默认只把歌词内嵌到音频标签里。在「设置」页的「下载设置」中可额外开启：

- **保存独立歌词文件 (.lrc)**：在音频同目录写出同名 `.lrc`，使用 UTF-8 BOM 编码，
  避免部分播放器按本地代码页把中文读成乱码。
- **另存翻译歌词 (.trans.lrc)**：额外写出 `.trans.lrc`；同时开启时，`.lrc` 会包含
  原文与翻译（原文在上，空行后接翻译），供支持的播放器显示双语。

若音频已存在但缺少歌词文件，会只补写 `.lrc` 而不重新下载音频。歌词文件已存在时，
需勾选「覆盖已存在文件」才会重写。

## 免责声明

本项目仅供学习和研究使用。请尊重版权、支持正版音乐，并遵守 QQ 音乐相关服务条款。禁止将本项目用于商业用途或任何侵权行为。
