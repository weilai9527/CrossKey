# CrossKey：Mac 控制 Windows

这是一个带配对认证的局域网输入转发工具：

```text
Mac 捕获键盘/鼠标事件 -> TCP 发送 -> Windows 接收 -> 模拟键盘/鼠标输入
```

使用 F8 快捷键切换控制模式，不传画面，也不是远程桌面。

## 文件

```text
mac_client.py    Mac 端，捕获输入并发送
win_server.py    Windows 端，接收事件并模拟输入，并通过 UDP 广播自己的地址
protocol.py      每行一个 JSON 的事件协议
auth.py          配对码 HMAC 挑战认证
settings.py      Mac/Windows 共用配置与校验
discovery.py     UDP 自动发现与广播
config.example.json  不含私密信息的配置示例
config.json      两端本地配置（自动创建，不提交到 Git）
configure_ip.py  手动修改 Windows IP/端口
configure_layout.py  在终端设置 Windows 相对 Mac 的屏幕位置
screen_edges.py  双向穿屏的边缘检测与落点比例映射
windows_monitors.py  Windows 显示设备编号、坐标与 DPI 处理
clipboard_sync.py    双向剪贴板同步、分块传输和接收确认
native_clipboard.py  macOS / Windows 原生剪贴板读写
clipboard_formats.py 图片、文件归档和富文本格式转换
check_connection.py  网络和配对诊断
requirements.txt Python 依赖
```

## 安装依赖

Mac 和 Windows 两边都需要 Python 3.10+。Windows 在项目目录执行：

```bash
python -m pip install -r requirements.txt
```

Mac 执行（macOS 通常没有 `python` 命令，需要使用 `python3`）：

```bash
python3 -m pip install -r requirements.txt
```

## Windows 端启动

在 Windows 上先运行：

```bash
python win_server.py
```

第一次启动会自动创建本地 `config.json`，生成六位配对码并显示在窗口中。Mac
必须使用相同配对码，配对码不会直接通过网络发送。两台机器使用各自的项目副本时，在
Mac 上运行下面的命令，并将示例数字替换成 Windows 窗口显示的数字：

```bash
python3 configure_ip.py --pairing-code 123456
```

看到类似输出：

```text
Windows Receiver started
Listening on 0.0.0.0:5050
Waiting for Mac connection...
```

如果 Windows 防火墙弹窗，允许 Python 访问专用网络。TCP 5050 用于控制连接；
Windows 会向 UDP 5051 发送自动发现广播。

查看 Windows 局域网 IP：

```powershell
ipconfig
```

通常找 `IPv4 Address`，例如：

```text
192.168.1.25
```

## Mac 端启动

默认会先通过 UDP 广播自动发现 Windows 接收端。Windows 端运行 `python win_server.py`
后，会显示：

```text
Broadcasting discovery on UDP port 5051
```

Mac 端运行 `python3 mac_client.py` 时，会先搜索这个广播；找到后自动连接，不需要手动改 IP。
如果连接过程中 Windows 端重启或网络短暂断开，Mac 端会暂停发送并自动尝试重新发现/连接。

如果自动发现失败，也可以用配置脚本手动修改 Windows IP：

```bash
python3 configure_ip.py
```

也可以直接用命令写入：

```bash
python3 configure_ip.py --host 192.168.1.25 --port 5050
```

配置会保存到 `config.json`：

```json
{
  "windows_host": "192.168.248.178",
  "port": 5050,
  "auto_discover": true,
  "discovery_port": 5051,
  "mouse_scale": 1.0,
  "auto_switch_edge": "off",
  "edge_monitors": {},
  "edge_margin": 2,
  "suppress_local_mouse": true,
  "map_cmd_to_ctrl": true,
  "debug_keys": false,
  "heartbeat_interval": 2.0,
  "heartbeat_timeout": 8.0,
  "sync_clipboard": true,
  "clipboard_only_control_mode": true,
  "clipboard_poll_interval": 0.7,
  "clipboard_max_bytes": 262144,
  "pairing_code": "123456"
}
```

然后在 Mac 上运行：

```bash
python3 mac_client.py
```

如果双击 `start_mac.command` 启动，它会先询问是否要修改 Windows IP/端口。
默认情况下不需要修改，只有自动发现失败或 Windows IP 固定时才需要手动配置。

也可以临时指定 IP：

```bash
python3 mac_client.py --host 192.168.1.25
```

临时指定 `--host` 会跳过 UDP 自动发现；如果想强制只用手动配置，也可以运行：

```bash
python3 mac_client.py --no-discover
```

## 使用

1. Windows 先运行 `python win_server.py`
2. Mac 再运行 `python3 mac_client.py`
3. Mac 按 `F8`，进入控制 Windows 模式
4. Mac 触控板移动、左右点击、键盘输入会发送到 Windows
5. 再按一次 `F8`，退出控制模式

### 双向鼠标穿屏

像设置扩展显示器一样在终端选择 Windows 位于 Mac 的哪一侧：

```bash
python3 configure_layout.py
```

按提示选择：

```text
1. Windows 在 Mac 右侧
2. Windows 在 Mac 左侧
3. Windows 在 Mac 上方
4. Windows 在 Mac 下方
5. 关闭边缘切换
6. 按 Windows 显示器编号映射多个边缘
```

也可以不进入交互界面，直接设置：

```bash
python3 configure_layout.py --position right --edge-margin 2
python3 configure_layout.py --position left --edge-margin 2
python3 configure_layout.py --position top --edge-margin 2
python3 configure_layout.py --position bottom --edge-margin 2
python3 configure_layout.py --position off
```

设置会保存到 `config.json`，重启 Mac 客户端后生效。对应的配置内容是：

```json
"auto_switch_edge": "right",
"edge_margin": 2
```

两端都更新到 v0.4.0 或更新版本并重启后，把鼠标向 Mac 主显示器右边缘继续推动，
就会自动进入 Windows，鼠标落在 Windows 主显示器左侧。控制 Windows 时，
把鼠标移到其左边缘并继续向左推动，就会自动返回 Mac。

只需在 Mac 设置布局；进入控制模式时会把布局发送给 Windows，无需两端分别配置。
四个方向的对应关系如下：

| Windows 相对 Mac 的位置 | 从 Mac 进入 | 从 Windows 返回 |
| --- | --- | --- |
| `right`（右侧） | Mac 右边缘 | Windows 左边缘 |
| `left`（左侧） | Mac 左边缘 | Windows 右边缘 |
| `top`（上方） | Mac 上边缘 | Windows 下边缘 |
| `bottom`（下方） | Mac 下边缘 | Windows 上边缘 |

两端按边缘上的相对位置映射鼠标落点，适配不同分辨率。落点稍微退入屏幕内部，
返回 Mac 后有 0.25 秒防抖，避免立即来回切换。返回前 Windows 会释放所有远程
按键和鼠标按钮；跨设备拖动不会延续为 Mac 上的拖动操作。

`F8` 始终可以手动切换。布局开启时，使用 `F8` 进入 Windows 也能从对应边缘返回，
但不会改变进入时 Windows 鼠标的位置。设为 `off` 关闭双向边缘切换（默认关闭）。
该功能需要 macOS 原生输入拦截，因此 `suppress_local_mouse` 必须保持为 `true`。
旧版 Windows 接收端仍可用 F8 返回，但不支持自动返回。

单方向模式默认使用两端的主显示器。穿屏使用 Mac 的鼠标/触控板；Windows 本机鼠标
不会反向控制 Mac。

### 多显示器映射：上方 1 号屏、右侧 2 号屏

此功能要求 Mac 和 Windows **都更新到 v0.5.0 或更新版本并重启**。Windows 使用
“扩展这些显示器”模式。在 Mac 执行：

```bash
python3 configure_layout.py --map top=1 --map right=2
python3 mac_client.py
```

如已保存手动 IP，也可以直接连接，跳过自动发现：

```bash
python3 mac_client.py --host 192.168.0.104
```

不要附加旧的 `--auto-switch-edge right` 参数，该参数会临时覆盖多屏映射。
配置保存到 Mac 本地 `config.json`，进入控制模式时自动传给 Windows：

```json
"auto_switch_edge": "off",
"edge_monitors": {"top": 1, "right": 2},
"edge_margin": 2
```

| Mac 出口 | Windows 入口 | 返回 Mac 的出口 |
| --- | --- | --- |
| 上边缘 | 1 号屏下方 | 1 号屏下边缘 → Mac 上方 |
| 右边缘 | 2 号屏左侧 | 2 号屏左边缘 → Mac 右侧 |

映射按显示器的完整桌面坐标计算，支持负坐标和不同分辨率；Windows 端启用每显示器 DPI
感知，使屏幕边界与鼠标坐标一致。Windows 两块屏之间仍按系统的排列移动，
从任一已映射屏幕的相邻边缘都能返回 Mac，不局限于最初进入的屏幕。
在 Mac 右上角触发时，优先选择移动量更大的方向；相同时优先上边缘。
多屏模式下 F8 进入上次使用的映射屏幕，首次默认选择上、右、下、左顺序中的第一个。

可在 Windows 查看程序识别到的屏幕编号：

```cmd
python win_server.py --list-monitors
```

输出含编号、设备名、分辨率、桌面位置和主屏标记。程序以 `\\.\DISPLAY1`、
`\\.\DISPLAY2` 等设备名的数字作为编号，不按枚举顺序编号。若 Windows 设置中的
“标识”编号与设备名不同，请以此命令的输出核对分辨率和位置，再调整 `--map`。
指定的屏幕不存在或屏幕信息读取失败时，本次控制会被拒绝并恢复 Mac 输入，不会跳到主屏。
连接旧版接收端时，Mac 会提示更新而不启用多屏控制。

每次 `--map` 命令替换整份映射；`--edge-margin` 单独使用时保留映射。
直接运行 `python3 configure_layout.py`，选择菜单 6 也可逐边设置屏幕编号。
改回单方向模式或关闭所有边缘切换：

```bash
python3 configure_layout.py --position right
python3 configure_layout.py --position off
```

上述 `--position` 命令会清除多屏映射。设置变更后重启 Mac 客户端。
Windows 屏幕信息会在每次进入时重新读取；在控制期间插拔屏幕或调整排列/缩放后，
先按 F8 返回再重新进入。Mac 端仍以主显示器为入口，更改 Mac 主显示器或分辨率后需重启客户端。

连接异常时，可以在 Mac 上检查网络和配对认证：

```bash
python3 check_connection.py
```

连接成功时，Mac 会显示：

```text
Connected and authenticated with Windows
macOS native event tap is ON: HID
```

Windows 会显示：

```text
Mac authenticated
```

如果 Mac 的 `F8` 被当成音乐/媒体键，改用这个备用快捷键：

```text
Control + Option + Command + 8
```

控制模式开启后，常用 Windows 快捷键会跟随键盘一起转发，例如：

```text
Control + C / Control + V
Option + Tab
F1 到 F12
```

默认还会把 Mac 的 `Command` 映射成 Windows 的 `Ctrl`，所以可以用 Mac 习惯的：

```text
Command + C / Command + V
```

如果你想让 `Command` 按原样作为 Windows 键发送，把 `config.json` 改成：

```json
"map_cmd_to_ctrl": false
```

如果快捷键仍然不对，可以临时打开按键日志：

```json
"debug_keys": true
```

## 剪贴板同步

从 v0.6.0 起，支持 Mac ↔ Windows 双向同步纯文本、图片、文件/文件夹和富文本。
**两台电脑都需要更新项目文件、重新安装 `requirements.txt` 中的依赖并重启程序**；
使用打包程序时，需要分别重新打包 Mac 和 Windows 版本。
新旧版本混用时仅同步文本，富文本退回纯文本，不发送图片或文件。

默认只有控制模式开启时才同步。复制后等待接收端显示 `Clipboard ... received`，
再在目标应用里粘贴；大文件需要等待传输完成。

- **图片**：复制截图或应用中的图片，在另一端的聊天窗口、文档或图片编辑器粘贴。
  Mac 可用 `Control + Shift + Command + 4` 将截图放到剪贴板；Windows 可用截图工具复制。
  图片统一通过 PNG 传输，Mac 同时提供 TIFF，Windows 同时提供 DIB 位图兼容格式。
  PNG 保留透明度；只接受传统 DIB 的应用会使用白色背景。
- **文件/文件夹**：在 Finder 或资源管理器复制实际存在的文件或文件夹，
  然后在另一端的 Finder／资源管理器目标目录粘贴，也可以粘贴到支持文件附件的应用。
  复制一张图片文件会按文件传输；要粘贴图片内容，请在图片查看器中复制图片。
  支持多选和嵌套目录，始终按复制处理，不删除或移动源文件。
- **富文本**：同步 HTML、RTF 以及应用提供的纯文本备用内容。
  在支持相应格式的应用中可保留粗体、颜色、链接、表格等样式，纯文本编辑器仅粘贴文字。
  实际样式由目标应用决定；不保证 Office 私有格式、RTFD 附件、外链图片或嵌入对象完整迁移。

可以在 `config.json` 里关闭：

```json
"sync_clipboard": false
```

如果想一直同步，不受 F8 控制模式影响，可以改成：

```json
"clipboard_only_control_mode": false
```

文本默认最多 256 KiB，可通过 `clipboard_max_bytes` 调整。
图片 PNG 最多 16 MiB、3200 万像素；富文本传输包最多 16 MiB；
文件归档和解包后的文件总量各最多 100 MiB，最多 2000 个文件/目录条目。
超过限制会跳过并显示原因，不会打印剪贴板内容。

接收文件先保存到系统临时目录下独立的 `crosskey-clipboard-*` 目录，校验完整后
才更新剪贴板。程序退出后暂存文件仍保留，方便继续粘贴；不再需要后可以手动删除这些目录。
文件内容和目录结构会复制，权限、扩展属性和资源分支不作跨系统保留。
暂不支持符号链接、特殊文件、尚未落盘的虚拟附件，以及 Windows 不接受的文件名
（例如 `:`、`?` 或保留设备名）；这些情况会明确跳过整个文件批次。

传输按 64 KiB 分块并进行 SHA-256 校验，收到完整数据后才写入剪贴板。
键鼠事件优先于 Mac 的文件发送队列；中断连接后重新传输当前剪贴板内容。
如果传输期间接收端又复制了新内容，会保留接收端的新内容；需要重新传输时请再次复制源内容。

测试：`python3 -m unittest discover -v`（Windows 使用 `python`）。
Mac 原生格式集成测试使用独立的私有测试剪贴板，不修改日常剪贴板：
`CROSSKEY_NATIVE_TEST=1 python3 -m unittest test_native_clipboard.MacNativeClipboardTests -v`。
Windows 的格式构造和资源释放有模拟测试，发布前仍应在 Windows 真机上分别验证
截图粘贴、资源管理器多文件/文件夹粘贴，以及浏览器或 Word 的富文本粘贴。

默认配置会在控制模式打开时拦截 Mac 本机鼠标和键盘，避免控制 Windows 时 Mac 屏幕跟着乱点：

```json
"suppress_local_mouse": true
```

退出控制模式或退出脚本后恢复；如果想关闭这个行为，把它改成 `false`。

## 断线保护

Mac 默认每 2 秒发送一次连接心跳。Windows 连续 8 秒收不到消息时会主动结束连接，
并释放所有由 Mac 按下的键和鼠标按钮。正常退出控制模式、Mac 断开和连接超时也会执行
同样的释放操作，避免 Ctrl、Shift 或鼠标按钮在 Windows 上卡住。

通常不需要修改；如有特殊网络环境，可以同时调整：

```json
"heartbeat_interval": 2.0,
"heartbeat_timeout": 8.0
```

`heartbeat_timeout` 必须大于 `heartbeat_interval`。

## 常见问题

### `zsh: command not found: python`

这是 macOS 没有 `python` 命令，不代表 Python 没安装。把命令中的 `python` 改成
`python3`。例如修改手动地址和配对码：

```bash
python3 configure_ip.py --host 192.168.1.25 --port 5050
python3 configure_ip.py --pairing-code 123456
```

每条配置命令成功后都会显示 `Saved Windows receiver`。如果终端先显示错误，配置文件
不会被修改。

### `Discovery timed out`

UDP 自动发现失败。程序会继续使用 `config.json` 中保存的 IP；随后出现
`Connected and authenticated with Windows` 就表示不影响正常使用。

自动发现依赖 Windows 发出的 UDP 5051 广播能够到达 Mac。VPN、虚拟网卡、公司/校园
Wi-Fi 的广播隔离、Mac 防火墙，或者 Windows 选择了错误的广播网卡，都可能使它失败。
Windows 上允许 UDP 5051 的入站规则通常不能解决这个方向的问题，因为 Windows 是发送方，
Mac 才是接收方。

自动发现失败时，先在 Windows 使用 `ipconfig` 查看实际 IPv4，然后在 Mac 保存：

```bash
python3 configure_ip.py --host WINDOWS_IP --port 5050
```

### `pairing code was rejected`

网络已经连通，但两端六位配对码不同。以 Windows 每次启动时显示的
`Pairing code` 为准，在 Mac 更新后重新连接：

```bash
python3 configure_ip.py --pairing-code 123456
python3 mac_client.py
```

上面的 `123456` 只是示例，必须替换成 Windows 窗口当前显示的数字。

### `timed out` 或 `Connection refused`

还没有建立 TCP 连接。确认 Windows 保持运行 `python win_server.py`，IP 没有变化，
Windows 防火墙允许 Python 的专用网络访问，并且两台设备处于同一局域网。随后在 Mac
运行：

```bash
python3 check_connection.py
```

诊断输出中的 `auto-discovered` 表示自动发现成功，`saved fallback` 表示正在使用手动
保存的 IP。

## macOS 权限

Mac 端需要开启辅助功能权限，否则无法稳定监听键盘鼠标：

```text
系统设置 -> 隐私与安全性 -> 辅助功能
```

把你运行 Python 的程序加入并打开权限，例如：

```text
Terminal
iTerm
Visual Studio Code
Python
```

如果权限刚打开后还没生效，重启终端再运行。

## 当前边界

这个 Demo 主要用于证明可行性，暂时不处理：

```text
Mac 多显示器入口、每个边缘的局部连接区域和物理尺寸校准
中文输入法完整同步
剪贴板中的虚拟文件、符号链接、Office 私有对象格式
托盘图标
```

如果鼠标速度不合适，可以调 `config.json`：

```json
"mouse_scale": 1.5
```

数值越大，Windows 鼠标移动越快。

注意：当前版本在 macOS 上使用系统事件层拦截本机输入。如果启动时提示无法创建 event tap，重新给 Terminal 开启辅助功能/输入监控权限后再试。

## 测试

项目使用 Python 自带的 `unittest`：

```bash
python3 -m unittest discover -v
```

测试覆盖协议边界、版本兼容、配对握手、共享配置、发现消息解析、四方向双向穿屏、
多屏编号映射、负坐标、共享边界、错误编号、不同分辨率落点、返回防抖、过期通知、
重连与按键释放，并包含已认证 socket 的多屏往返集成测试。
测试中的鼠标/键盘控制器使用替身；真实 macOS 权限、Windows 显示缩放与跨机器手感仍需真机验收。

## 打包

安装运行依赖和打包工具：

```bash
python3 -m pip install -r requirements-build.txt
```

必须在目标系统本机打包：

```bash
# 在 macOS 上
python3 build_executable.py mac

# 在 Windows 上
python build_executable.py windows
```

单文件程序输出到 `dist/`。macOS 版本仍需要辅助功能和输入监控权限；Windows
版本首次监听网络时仍需允许防火墙访问局域网。
