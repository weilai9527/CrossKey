# Mac 控制 Windows 最小 Demo

这是一个局域网输入转发 Demo：

```text
Mac 捕获键盘/鼠标事件 -> TCP 发送 -> Windows 接收 -> 模拟键盘/鼠标输入
```

第一版只做 F8 快捷键切换，不传画面，也不是远程桌面。

## 文件

```text
mac_client.py    Mac 端，捕获输入并发送
win_server.py    Windows 端，接收事件并模拟输入
protocol.py      每行一个 JSON 的事件协议
config.json      Mac 端默认连接配置
requirements.txt Python 依赖
```

## 安装依赖

Mac 和 Windows 两边都需要 Python 3.10+，然后在项目目录执行：

```bash
python -m pip install -r requirements.txt
```

## Windows 端启动

在 Windows 上先运行：

```bash
python win_server.py
```

看到类似输出：

```text
Windows Receiver started
Listening on 0.0.0.0:5050
Waiting for Mac connection...
```

如果 Windows 防火墙弹窗，允许 Python 访问局域网。

查看 Windows 局域网 IP：

```powershell
ipconfig
```

通常找 `IPv4 Address`，例如：

```text
192.168.1.25
```

## Mac 端启动

先把 `config.json` 里的 `windows_host` 改成 Windows 的局域网 IP：

```json
{
  "windows_host": "192.168.248.178",
  "port": 5050,
  "mouse_scale": 1.0,
  "suppress_local_mouse": true
}
```

然后在 Mac 上运行：

```bash
python mac_client.py
```

也可以临时指定 IP：

```bash
python mac_client.py --host 192.168.1.25
```

## 使用

1. Windows 先启动 `win_server.py`
2. Mac 再启动 `mac_client.py`
3. Mac 按 `F8`，进入控制 Windows 模式
4. Mac 触控板移动、左右点击、键盘输入会发送到 Windows
5. 再按一次 `F8`，退出控制模式

如果 Mac 的 `F8` 被当成音乐/媒体键，改用这个备用快捷键：

```text
Control + Option + Command + 8
```

默认配置会在控制模式打开时拦截 Mac 本机鼠标和键盘，避免控制 Windows 时 Mac 屏幕跟着乱点：

```json
"suppress_local_mouse": true
```

退出控制模式或退出脚本后恢复；如果想关闭这个行为，把它改成 `false`。

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
鼠标穿屏
多显示器坐标映射
中文输入法完整同步
剪贴板同步
加密认证
托盘图标
```

如果鼠标速度不合适，可以调 `config.json`：

```json
"mouse_scale": 1.5
```

数值越大，Windows 鼠标移动越快。

注意：当前版本在 macOS 上使用系统事件层拦截本机输入。如果启动时提示无法创建 event tap，重新给 Terminal 开启辅助功能/输入监控权限后再试。
