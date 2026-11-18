CrossKey v0.7.1 修复虚拟机网卡导致地址识别错误、自动发现失败的问题。

- Windows 通过系统网卡属性识别已连接的物理网卡，地址列表排除 VMware、VirtualBox、Hyper-V、WSL、VPN 等虚拟网卡。
- 自动发现从每张物理网卡分别向对应子网广播，避免全局广播被路由到虚拟网卡。
- 单张网卡发送失败不影响其他网卡；每 30 秒重新检测，适应 Wi-Fi 与有线网络切换。
- 检测失败时提示检查网络，不回退到未经筛选的虚拟网卡地址。手动指定地址仍可使用。
- 保留 v0.7.0 的桌面启动窗口、配对认证、键鼠控制、剪贴板同步和用户配置。

源码升级：退出旧 CrossKey 程序，在项目目录运行 `git pull --ff-only`，然后重新运行
Windows 的 `start_windows.bat` 或 Mac 的 `start_mac.command`。已有 `config.json` 会保留。
如果此前手动保存了虚拟网卡地址，请在设置中启用自动发现或改填物理网卡地址。

Windows 便携包：下载并解压整个 `CrossKey-Windows-v0.7.1.zip`，退出旧版后运行新版
`CrossKey-Windows.exe`，无需安装 Python。保留 `%APPDATA%\CrossKey\config.json` 即可保留配置。

测试覆盖虚拟网卡排除、多物理网卡广播、网卡切换、错误隔离及服务停止。
真实用户设备上的键鼠、多屏和网络连通性仍需实际环境确认。
