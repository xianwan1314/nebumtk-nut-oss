# 个人版构建手册（无提权 / 假设已有 root）

这份文档记录当前工作区里“从 0 到 1 打出 `NEBUMTK_UNLOCK_TOOLS.exe`”的实际步骤。
它对应的是**个人构建**，不是仓库默认的发行流程：运行时不做 payload 提权，不安装
root，SakiSU 页签隐藏，`material/xingc` 随包放在 exe 旁边。

> 适用范围：Windows 10/11 x64、Python 3.11、Nuitka standalone、PySide6。
> 目标读者：以后要自己重新打包的人。

---

## 0. 最终产物长什么样

```text
dist/nebumtk_unlock_tools.dist/
  NEBUMTK_UNLOCK_TOOLS.exe          # 入口；必须和同目录 DLL/数据一起分发
  README-UNLOCK.txt                 # 使用说明
  LICENSE / COPYRIGHT / NOTICE.md   # AGPL 要求
  platform-tools/                   # adb.exe / fastboot.exe + DLL
  misc/                             # 品牌 misc 镜像（Nuitka 数据目录）
  material/xingc/                   # Xingc catalog + 私钥 + AVB 公钥 + permit
  images/                           # lk.img / lk_xingc.img / receipt
  PySide6/  shiboken6/  ...         # Qt 运行时
  nebumtk_unlock_tools/lk/libusb/_platform/_windows/x64/libusb-1.0.dll
```

**整个 `.dist` 文件夹一起复制**；只拿 exe 会缺少 Qt DLL、platform-tools 和 misc。

---

## 1. 前置条件

| 项 | 要求 | 说明 |
|---|---|---|
| Windows | 10/11 x64 | 项目只支持 Windows |
| Python | 3.11.x | 当前构建使用 3.11.5 |
| MSVC | Visual Studio 2022 Build Tools，`cl 14.5` | Nuitka 的 C 编译器；本工作区 `build.ps1` 已去掉 `--zig`，没有 MSVC 时把它加回 `--zig` 即可（会自动下载 Zig） |
| Nuitka | 4.2.1 | `pip install nuitka ordered-set zstandard` |
| PySide6 | 6.8.3 或 requirements 里的 6.10.2 | 构建机实际用的是 6.8.3 |
| platform-tools | 仓库自带 `platform-tools/` | 构建脚本会复制到 dist |
| LK 镜像 | `imags/lk.img` | 用于生成 `material/xingc` 和 patched LK |
| 网络 | 可选 | 首次装依赖、Nuitka 下载 depends 时需要 |

检查 Python / Nuitka：

```powershell
python --version
python -c "import PySide6; print(PySide6.__version__)"
python -c "import nuitka.Version as v; print(v.getNuitkaVersion())"
```

Nuitka 能识别 MSVC 即可；`where cl` 找不到是正常的，Nuitka 会通过 vswhere 找。

---

## 2. 准备 Python 环境

推荐 venv：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pip install nuitka ordered-set zstandard
```

当前这台机器没有走 venv，直接用系统 Python 3.11；两种都可以，只要
`build.ps1` 调用到的 `python` 能找到依赖。

如果 `pip` 卡住，换用可访问的镜像/网络重试即可；依赖最终应装在当前 Python 的
用户级 `site-packages` 里。

---

## 3. 准备 Xingc 素材

如果已经有 `material/xingc/`，跳过本节。

从原厂 LK 生成：

```powershell
python tools\prepare_xingc_lk.py --lk imags\lk.img --dry-run
python tools\prepare_xingc_lk.py --lk imags\lk.img
python tools\prepare_xingc_lk.py --lk imags\lk.img --check
```

生成：

```text
material/xingc/patch-catalog.json
material/xingc/protocol-permit
material/xingc/verify-private.pem      # 私钥，务必备份
material/xingc/verify-public.avbpub
imags/lk_xingc.img
imags/lk_xingc.img.receipt.json
```

`verify-private.pem` 只对这份原厂 LK 有效；换 LK 要重新走
`--regenerate-key` 并重打 patched LK。

---

## 4. 准备 PyUSB / libusb（Xingc 解锁需要）

安装到用户级 site-packages：

```powershell
python -m pip install pyusb==1.3.1 libusb==1.0.29
```

本机已经安装成功；不需要额外的本地依赖目录，也不需要设置 `PYTHONPATH`。

验证：

```powershell
python -c "import usb, libusb; print(usb.__version__); print(libusb.__file__)"
python -c "from nebumtk_unlock_tools.lk.usb_backend import get_libusb_backend; print(get_libusb_backend())"
```

PyPI 的 `libusb` DLL 在 `libusb\_platform\_windows\<arch>\`；`usb_backend` 已支持
这个路径，不需要手动复制 DLL。

---

## 5. 生成/重建机型目录（本构建实际做过）

仓库自带的 `embedded_catalog.py` 带有旧版“密码派生 preload-pack”字段，
`tools/build_device_catalog.py` 默认校验会拒绝它。个人构建用本地密钥重新签名：

```powershell
python tools\build_device_catalog.py --init-key --regenerate
python tools\build_device_catalog.py   # 验证，退出码 0
```

会生成/更新：

```text
local-dev/catalog_signing_private.pem
local-dev/catalog_content_key.bin
local-dev/catalog_preload_key.bin
material/preload-pack.key
nebumtk_unlock_tools/resources/embedded_catalog.py   # 重新生成，勿手改
```

结果：仍是 23 个机型档案（20 vivo / 2 OPPO / 1 Honor），但信任根换成了
`local-dev` 里的公钥。**`local-dev/` 不要随发行包分发**；以后要加机型/重签
catalog 时还要用到它。

验证：

```powershell
python -c "from nebumtk_unlock_tools.resources.catalog import load_embedded_device_catalog as l; c=l(); print(len(c['devices']), c['revision'])"
# 期望：23 1
```

---

## 6. 关键环境变量（Nuitka 在本机踩过的坑）

本机的 Nuitka 启动会卡住，原因是：

- 默认会检查更新，网络/代理下可能挂起；
- 默认缓存目录在 `%LOCALAPPDATA%\Nuitka`，当前沙箱/ACL 下写入被拒绝，表现为
  `SConsLockFailure: Timeout waiting for lock ...`。

解决：把更新检查关掉，把缓存和临时目录放到工作区：

```powershell
$env:NUITKA_UPDATE_CHECK = "never"
$env:NUITKA_CACHE_DIR    = "$PWD\.nuitka-cache"
$env:TEMP                = "$PWD\.tmp"
$env:TMP                 = $env:TEMP
New-Item -ItemType Directory -Force -Path $env:NUITKA_CACHE_DIR,$env:TEMP | Out-Null
```

如果仍然看到 `SConsLockFailure`：

```powershell
Get-Process python -ErrorAction SilentlyContinue | Stop-Process -Force
Remove-Item .nuitka-cache -Recurse -Force -ErrorAction SilentlyContinue
# 重新创建目录后再构建
```

---

## 7. 本构建对源码做的改动

如果从**原始仓库**开始，需要先做这些改动，否则打出来的 exe 仍会带提权入口。
本工作区已经改好，下面是清单。

### 7.1 `flows/base.py`：只接受已有 root

- `ensure_root()` 只探测 `adb.su_path` / 机型 `su_path` / `su`，**连续两次**
  `su -c id` 都必须是 uid 0；没有 root 就抛 `VerificationError`。
- `escalate_root()` 直接抛异常，不再推送 payload。
- 不再调用 `RootEscalator.escalate()`。

### 7.2 `flows/stages.py`

- `RootEscalationFlow.run()` 改为 `ensure_root()`。
- `VivoCompleteUnlockFlow.run()`：
  - 只要求 `xingc` capability（不再要求 `preload`）；
  - `escalate_root()` 改为 `ensure_root()`；
  - `install_sakisu_root=True` 时直接报错拒绝（本构建不写 root 镜像）。

### 7.3 UI

- `gui/pages/root_unlock.py`：临时 Root 段隐藏、按钮禁用；SakiSU 勾选框隐藏。
- `gui/pages/images_recovery.py`：SakiSU 页签隐藏，默认落在 LK 工具页签。
- `gui/main_window.py`：一键 Vivo 不再要求 preload；不传 SakiSU 安装参数；
  SakiSU 勾选框禁用。
- `config/store.py`：**首次运行（没有 `settings.json`）默认开启测试模式**，
  顶部显示机型/运行模板；已有 settings.json 保留原值。

> 注意：payload / SakiSU 的底层代码仍然保留，只是运行时不可达。这样改动最小，
> 也方便以后恢复；如果你要彻底删除，需要同步清理 `core/root.py`、
> `resources/payload_store.py`、`sakisu/` 以及相关测试。

### 7.4 构建脚本与审计

- `scripts/build.ps1`：
  - 去掉 `--zig`（本机用 MSVC `cl 14.5`）；
  - 增加 `--include-package=usb --include-package=libusb --include-package-data=libusb`；
  - SakiSU 二进制不存在时只提示，不再让构建失败；
  - **不再调用 `tools/audit_distribution.py`**，个人构建允许把 `material/xingc`
    放在 exe 旁边。
- `tools/audit_distribution.py` 文件**保留**，供以后公开发布时手动跑；它不是
  exe 的运行时依赖。
- `.gitignore`：加入 `.nuitka-cache/`、`.tmp/`。

---

## 8. 正式构建

在仓库根目录：

```powershell
$env:NUITKA_UPDATE_CHECK = "never"
$env:NUITKA_CACHE_DIR    = "$PWD\.nuitka-cache"
$env:TEMP                = "$PWD\.tmp"
$env:TMP                 = $env:TEMP
New-Item -ItemType Directory -Force -Path $env:NUITKA_CACHE_DIR,$env:TEMP | Out-Null

powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build.ps1 -SkipInstall
```

个人构建的 `build.ps1` **不再调用 audit**，所以即使你后面把 `material/xingc` 放进
dist，也不会再出现 `forbidden file`。`tools/audit_distribution.py` 仍保留在仓库里，
需要做公开发布审计时可以手动跑。

说明：

- `-SkipInstall` 表示依赖已经装好，不再 `pip install`；否则脚本会先装
  `requirements.txt` 和 `nuitka ordered-set zstandard`。
- 构建脚本会做：
  1. 校验 `BUILD_CHANNEL=offline`；
  2. `tools/build_device_catalog.py` 验证签名目录；
  3. 没有 `material/payloads/manifest.json` 就跳过 payload store（本构建没有）；
  4. Nuitka `--standalone --enable-plugin=pyside6`，包含 `misc`、`usb`、`libusb`；
  5. 复制 `LICENSE`/`COPYRIGHT`/`NOTICE.md`、`platform-tools`、libusb DLL、
     `README-UNLOCK.txt`、`UNLOCK-GUIDE.md`、`release-channel.txt`；
  6. **不跑 audit，也不写 `release-sha256-manifest.json`**；
  7. 启动一次打包 smoke（10 秒）。
- 本机第一次构建时 smoke 清理的 `taskkill` 报 `Access denied`。这是清理阶段的权限问题，
  不影响已经写出的 exe；脚本现在末尾会 `exit 0`。遇到同样情况可以：
  - 加 `-SkipLaunchSmoke` 跳过 smoke；
  - 或手动启动 exe 验证后，继续第 9 节。

---

## 9. 组装最终 dist

个人构建不再跑 audit，直接把 `material/xingc` 和 `images` 放进去即可：

```powershell
$dist = "dist\nebumtk_unlock_tools.dist"
New-Item -ItemType Directory -Force -Path "$dist\material\xingc","$dist\images" | Out-Null
Copy-Item material\xingc\* "$dist\material\xingc\" -Force
Copy-Item imags\lk.img,imags\lk_xingc.img,imags\lk_xingc.img.receipt.json "$dist\images\" -Force
```

`README-UNLOCK.txt` 和 `UNLOCK-GUIDE.md` 已由 `build.ps1` 自动复制，不需要再手动拷。

> 这个 dist 带私钥，只适合个人使用。以后要公开发布时，把 `material` 移出，并手动
> 运行 `python tools\audit_distribution.py dist\...`；当前 `build.ps1` 不再替你跑
> 这一步。

---

## 10. 验证

### 10.1 构建产物

```powershell
Get-FileHash dist\nebumtk_unlock_tools.dist\NEBUMTK_UNLOCK_TOOLS.exe -Algorithm SHA256
Get-ChildItem dist\nebumtk_unlock_tools.dist -Recurse -File | Measure-Object Length -Sum
```

个人构建不再自动跑 `audit_distribution`；如果你需要公开发布，再手动执行：

```powershell
python tools\audit_distribution.py dist\nebumtk_unlock_tools.dist
```

（带 `material/xingc` 的目录会被它拒绝，这是公开发布前必须移出私钥的原因。）

### 10.2 素材 / LK / Xingc 链路

```powershell
$env:NUT_MATERIAL_ROOT="$PWD\dist\nebumtk_unlock_tools.dist\material"

python -c "from nebumtk_unlock_tools.lk.keys import verify_key_pair; verify_key_pair(); print('key pair ok')"
python -c "from pathlib import Path; from nebumtk_unlock_tools.lk.ubl_service import InternalUblPatcher; InternalUblPatcher().validate_patched(Path('dist/nebumtk_unlock_tools.dist/images/lk_xingc.img')); print('patched LK ok')"
python -c "from nebumtk_unlock_tools.lk.xingc_service import XingcUnlockService; XingcUnlockService.readiness_check(); print('xingc readiness ok')"
```

期望：

```text
key pair ok
patched LK ok
xingc readiness ok
```

### 10.3 GUI 冒烟

```powershell
$env:QT_QPA_PLATFORM="offscreen"
python -c "import sys; from pathlib import Path; from PySide6.QtWidgets import QApplication; app=QApplication(sys.argv); from nebumtk_unlock_tools.gui.main_window import MainWindow; w=MainWindow(Path('.').resolve(), Path('workspace').resolve()); print('pages', w.pages.count()); print('root', w.authorized_page.privilege_section.isVisible()); print('sakisu_tab', w.images_page.task_tab_buttons[0].isVisible()); w.device_scan_timer.stop(); w.close(); print('ok')"
```

期望：`pages 5`、`root False`、`sakisu_tab False`。

### 10.4 打包 exe 冒烟

在目标机器上直接双击 `NEBUMTK_UNLOCK_TOOLS.exe`；或：

```powershell
Start-Process .\dist\nebumtk_unlock_tools.dist\NEBUMTK_UNLOCK_TOOLS.exe
```

程序应正常打开五页窗口。真机写入/解锁必须另做设备验证。

---

## 11. 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| `python -m nuitka --version` 卡住 | 更新检查/网络挂起 | 设 `$env:NUITKA_UPDATE_CHECK="never"` |
| `SConsLockFailure: Timeout waiting for lock ...` | Nuitka 缓存目录不可写/被占用 | 设 `NUITKA_CACHE_DIR` 到工作区；杀掉残留 `python`；删 `.nuitka-cache` 重来 |
| `pip install` 卡住 | 网络/镜像不可达 | 换可访问的镜像/网络重试；依赖最终装在用户级 `site-packages` |
| `No module named 'usb'` / `No backend available` | 没装 PyUSB/libusb，或 DLL 不在候选路径 | 按第 4 节准备；确认 `libusb-1.0.dll` 在 `_platform\_windows\x64` |
| `No module named 'pkg_about'` | 完整 `libusb` 包的元数据依赖 | 用最小 `libusb` shim（第 4 节） |
| `no known Xingc command context` | LK 不是已知的 Vivo `veify` 布局 | 用 `prepare_xingc_lk.py --catalog` 指定审核过的目录 |
| `SakiSU host patcher absent` | GitHub 下载失败/未提供上游产物 | 本构建隐藏 SakiSU 页签；要恢复需提供 APK/host exe |
| 构建打印 Done 但退出码 1（旧版脚本） | 脚本末尾没有 `exit 0`，PowerShell 继承了上一条原生命令的退出码 | 现在脚本已加 `exit 0`；仍以 `Done:` 和 exe 是否存在为准 |
| 手动跑 `audit_distribution` 报 `forbidden file: ...verify_private.pem` | 私钥素材已经在 dist 里 | 这是预期的；公开发布前把 material 移出 |
| 手动跑 `audit_distribution` 报 `missing release manifest` | 个人构建不再自动写 manifest | 需要发行时先跑 `--write-manifest` 或恢复发行构建流程 |
| 双击 exe 没反应/缺 DLL | 只复制了 exe，没复制整个 `.dist` | 整个目录一起复制 |
| 设备未 root 时“准备解锁”报错 | 本构建不做提权 | 先用你自己的方式取得 root，确认两次 `adb shell su -c id` 都是 uid=0 |

---

## 12. 重建与清理

保留这些，方便下次构建：

```text
local-dev/        # catalog 签名/内容/preload 密钥
material/xingc/   # Xingc 素材
imags/            # 原厂与 patched LK
```

PyUSB/libusb 已经装在当前 Python 的用户级 `site-packages` 里，不需要额外的本地
依赖目录。

可以删除/重建：

```text
dist/             # Nuitka 输出，--remove-output 会清掉
.nuitka-cache/    # Nuitka 缓存
.tmp/             # Nuitka 临时目录
workspace/        # 源码运行/测试时产生
```

下次构建重复第 6～9 节即可；如果只改 Python 代码，不需要重新生成 catalog。

---

## 13. 边界与合规

- 本构建**不提供**持久 root、payload 提权、SakiSU/Magisk/KernelSU 安装。
- `material/xingc/verify-private.pem` 是签名私钥；放在 dist 里只适合个人使用。
- 对外分发时必须保留 `LICENSE`、`COPYRIGHT`、`NOTICE.md` 和 exe 内的
  provenance 标记；个人构建不再自动跑 audit，公开发布前要手动跑
  `tools/audit_distribution.py`。
- 只对你拥有或获授权的设备操作；写 LK/misc 有变砖风险。
- 完整 `pytest` 本次没有跑（本机无 pytest，pip 网络不稳定）。改动只关掉了
  提权/SakiSU 入口，相关旧测试仍可能按“应提权/应安装”断言而失败；如果要维护
  测试，需要同步删改 `tests/test_root_*`、`tests/test_offline_preload_grant.py`、
  `tests/test_sakisu_*` 等。
