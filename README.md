# QQ 群相册下载与 EXIF 匹配器

这是一个面向 Windows 的本地 QQ 群相册下载工具。它的主要工作是：读取当前账号有权限访问的群相册，下载 QQ 接口返回的图片或视频资源，分析下载文件中的 EXIF，并把 QQ 服务端提供的日期、相机和 GPS 信息作为缺失字段的补充来源。

本项目不是照片管理器，也不保证恢复相机中的原始文件。QQ CDN 可能只提供压缩版本，下载文件也可能已经被 QQ 重新编码或清理过 EXIF。程序会把文件本身读到的 EXIF、QQ 服务端的 `mapExifInfo`、接口拍摄时间和上传时间分开记录，避免把它们混称为“原始信息”。

## 项目来源

本项目在 QQ 群相册接口探索、资源地址选择、批量下载以及图片/视频处理方向上，参考并部分借鉴了 [lihengdao666/QQGroupAlbumDownload](https://github.com/lihengdao666/QQGroupAlbumDownload) 的公开内容。

本项目不是该项目的官方版本，也不是官方维护的 Fork。当前代码根据本项目需求进行了独立的 Python、本地 Web、SQLite、下载完整性检查、EXIF 字段匹配、日期预演和人工复核实现。参考项目的归属与许可说明见 [`NOTICE.md`](NOTICE.md)。

## 当前能做什么

- 通过二维码登录或 QQNT 本地快速登录取得当前会话。
- 按 QQ 群号读取相册列表，并扫描选中相册的照片和视频元数据。
- 识别 QQ 接口返回的图片资源和视频资源，支持常见图片、MP4、MOV、M4V、AVI、MKV 等媒体类型。
- 优先使用接口明确标记的原图地址；没有明确原图标记时，使用接口可获得的最高分辨率版本。
- 校验 HTTP `Content-Length`、图片解码结果和 JPEG 结束标记，减少截断文件进入归档的情况。
- 下载和分析阶段支持 `1/2/4/8/16/24/32` 个线程，整理副本阶段使用同一线程设置并发复制和写入 EXIF。
- 记录文件 EXIF、QQ 服务端 EXIF、接口拍摄时间、上传者、上传时间、CDN 尺寸、媒体类型和 SHA-256。
- 对照片日期生成可解释的预演结果，显示日期来源、精度、可信度、冲突和批次锚点。
- 支持待复核筛选、单张确认、批量接受当前建议、失败记录筛选和 CSV 导出。
- 可选生成 `01_library` 整理副本；原始下载副本保存在 `00_raw`，不会被写入 EXIF。
- 页面提供初始化、彻底清空本地数据、停止任务和关闭本地后台服务功能。

## 日期匹配规则

当前日期来源优先级如下：

```text
文件 DateTimeOriginal                 FILE_EXIF
    > 文件 CreateDate/DateTimeDigitized FILE_SECONDARY
    > QQ mapExifInfo["11"]              QQ_MAP_EXIF
    > QQ 接口拍摄时间                    QQ_SHOOT_TIME
    > 同一上传批次的日期锚点推导          BATCH_NEIGHBOR / BATCH_SINGLE_DATE
    > QQ 上传日期                        QQ_UPLOAD_TIME
```

具体行为：

- 文件 EXIF、QQ 服务端 EXIF 和接口拍摄时间会保留到秒，但程序不会把下载时间或程序运行时间当作拍摄时间。
- 同一相册、同一上传者、相邻上传间隔不超过 10 秒的记录会被视为一个候选上传批次。
- 缺少日期的照片，只有在前后两个日期锚点属于同一天时，才会自动采用 `BATCH_NEIGHBOR`。
- 批次只有一个日期锚点时使用 `BATCH_SINGLE_DATE`，但默认标记为待复核。
- 批次锚点出现多个日期时不进行多数投票，通常回退到 QQ 上传日期并标记冲突或待复核。
- QQ 上传日期只表示“QQ 在该日期记录了上传”，不等同于真实拍摄日期。
- 批次推导和上传日期只精确到日，生成整理副本时使用当天 `12:00:00` 作为写入值。
- 没有可信拍摄日期或上传日期的记录标记为 `UNKNOWN`，不会自动生成整理副本。
- QQ CDN 的部分 JPEG 会在主图结束标记后附加扩展数据；下载校验会扫描整个文件，避免把这类可正常解码的文件误判为截断。

人工确认后的记录会使用 `MANUAL_REVIEW` 来源，并保留人工确认日期和说明。

## EXIF 字段规则

相机厂商、相机型号和 GPS 按字段分别选择来源：

```text
文件 EXIF > QQ_MAP_EXIF > 空值
```

也就是说，QQ 服务端数据只补文件中缺失的字段，不覆盖文件已有的设备或 GPS 信息。QQ 没有提供的字段保持为空。

启用页面中的“补全整理副本 EXIF”后，程序只对 `01_library` 副本调用 ExifTool：

- 图片：尽量写入缺失的 `DateTimeOriginal`、`CreateDate`、XMP 日期、相机厂商、型号、ISO、曝光、光圈、焦距和 GPS；
- 视频：写入 QuickTime 创建日期字段；
- 已存在的可识别字段不会被覆盖；
- 不生成额外的 `.xmp` 旁车文件；
- `00_raw` 不执行 EXIF 写入。

如果没有检测到 ExifTool，程序仍可以保留下载文件、数据库和 Windows 文件时间，但不能保证媒体内部出现新的 EXIF 日期或设备字段。

## 文件名与目录

程序不承诺恢复 QQ 接口中的真实文件名，例如 `IMG_0034.JPG`。文件名使用稳定记录键，便于重新扫描和续传：

```text
00_raw/<群号>/<相册>/QQ_<record_key>.<扩展名>
01_library/<群号>/<相册>/YYYY-MM-DD__QQ_<record_key>.<扩展名>
```

默认归档目录：

```text
%USERPROFILE%\Pictures\QQGroupAlbumArchive
```

目录结构：

```text
QQGroupAlbumArchive/
├─ 00_raw/       程序下载并分析的媒体副本
├─ 01_library/   可选生成的日期副本和 EXIF 副本
├─ metadata/     SQLite、JSON、CSV 元数据清单
└─ reports/      缺少日期、待复核、失败和媒体类型报告
```

重新下载时，如果发现现有文件损坏、扩展名与媒体类型不一致或属于旧的封面文件，程序可能先将其重命名为 `__corrupt` 或 `__legacy`，再下载新的文件。这不是重复识别功能，而是下载完整性修复的一部分。

## 使用方式

### Windows 单文件版

双击 `QQAlbumArchiver.exe` 后，程序会启动本地服务并自动打开：

```text
http://127.0.0.1:8767/
```

单文件版内嵌 Python、Pillow 和 ExifTool，不需要另外安装这些组件。登录会话和运行时数据库保存在：

```text
%LOCALAPPDATA%\QQAlbumArchiver\work
```

完成测试后使用页面右上角的“关闭后台”，避免后台进程继续占用端口。

### 源码运行

要求：Windows、Python 3.11 或更高版本。

```powershell
python -m pip install Pillow
python app_web.py
```

也可以双击 `start_web.cmd`。程序启动后打开 `http://127.0.0.1:8767/`。

推荐操作顺序：

1. 登录 QQ；
2. 输入群号并读取相册；
3. 选择相册并扫描元数据；
4. 点击“下载并分析原图”，等待媒体进入 `00_raw`；
5. 查看日期预演，处理待复核记录；
6. 按需点击“生成整理副本”，得到 `01_library`。

“下载并分析原图”不会生成 `01_library`；“生成整理副本”也不会修改 `00_raw` 中的 EXIF 内容。
如果少量记录下载失败，生成整理副本仍会处理已完成的记录，并在任务状态中列出未下载跳过数量；失败记录可在“下载失败”筛选中查看，再次执行下载分析时会重试未完成记录并跳过已完成记录。

## 页面之外的导入能力

核心模块还保留 JSON、CSV 和 HAR 元数据导入能力，以及一个兼容性的命令行/旧版 GUI 入口。它们适合导入已有采集结果或诊断接口，不是当前网页主流程的首选方式。

示例：

```powershell
python qq_album_archiver.py `
  --no-gui `
  --workspace D:\QQAlbumWork `
  --import D:\qq-album.json `
  --source D:\QQDownloads `
  --archive D:\QQArchive `
  --process `
  --download
```

项目中的 `qq_group_album_collector.user.js` 可以在已经登录的 QQ 群相册页面中采集页面已加载的元数据和资源地址。HAR 文件可能包含敏感请求头，导入前应确认来源并在使用后删除原始 HAR。

## 开发、测试与构建

运行测试：

```powershell
python -m unittest discover -s tests -v
```

当前测试覆盖日期预演、EXIF 来源优先级、QQ 服务端字段解析、视频 URL、下载截断检测、人工复核、数据库状态和整理副本并发线程数。

重新构建单文件版：

```powershell
python -m pip install Pillow pyinstaller
python -m PyInstaller --clean --noconfirm --onefile --noconsole `
  --name QQAlbumArchiver `
  --add-data "tools\exiftool;tools\exiftool" `
  --paths . app_web.py
```

生成文件位于 `dist/QQAlbumArchiver.exe`。`build/`、`dist/`、`release/`、本地数据库、登录会话和缓存均被 `.gitignore` 排除；正式发布建议把 EXE 放在 GitHub Releases，而不是提交进源码仓库。

## 安全边界与已知限制

- 只能下载当前账号有权限访问的群相册内容。
- 登录流程不要求本程序读取 QQ 密码，也不使用进程内存扫描或客户端 Cookie 数据库解密。
- QQ 登录接口、群相册接口、CDN 参数和页面结构可能变化，登录或下载失效时需要重新适配。
- QQ 明确返回“原图候选”不等于文件一定保留了相机原始 EXIF；程序会分别保存文件 EXIF 和 QQ 服务端 EXIF，供后续判断。
- 最高分辨率版本可能仍然是压缩图；程序不伪造 `IMG_0034.JPG` 等真实原文件名。
- 不同 QQ 记录可能对应相同文件内容，这是 QQ/CDN 层面的实际重复，不属于本程序的重复识别结果。
- 本项目不绕过 QQ 权限控制，也不保证获取账号无权访问的内容。

## 致谢与第三方组件

- [QQGroupAlbumDownload](https://github.com/lihengdao666/QQGroupAlbumDownload)：QQ 群相册接口、资源选择和批量下载方向的参考项目。
- [ExifTool](https://exiftool.org/)：用于读取和写入媒体元数据；许可文件位于 `tools/exiftool/`。
- [Pillow](https://python-pillow.org/)：用于图片解码和完整性检查。

具体归属和第三方许可文本见 [`NOTICE.md`](NOTICE.md)。

## 许可证

本项目采用 [MIT License](LICENSE)。第三方组件和参考项目的许可说明见 [`NOTICE.md`](NOTICE.md) 及各自目录中的许可文件。
