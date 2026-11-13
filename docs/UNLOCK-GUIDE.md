# NEBUMTK 解锁操作手册（个人无提权构建）

这份手册对应 `dist/nebumtk_unlock_tools.dist/` 里的 exe。它假设：

- 目标设备是 **vivo PD2417 / iQOO Z9 Turbo+（MT6989）**，或至少是这份
  `images/lk.img` 对应的同一固件；
- 设备上**已经存在可用的 root**（本构建不做 payload 提权）；
- 你接受解锁会清空数据、写 bootloader 分区有变砖风险。

> 如果设备型号/固件不是这一份，先看第 7 节，不要直接刷 `images/lk_xingc.img`。

---

## 0. 复制到另一台电脑能不能直接运行？

**可以，但必须复制整个 `nebumtk_unlock_tools.dist` 文件夹，不能只拿 exe。**

原因：exe 是 Nuitka standalone，Python 解释器、Qt/PySide6、platform-tools、
`misc`、`material` 都以文件形式放在同目录；只拿 exe 会缺 DLL/数据，启动即失败。

### 目标电脑的要求

| 项 | 要求 |
|---|---|
| 系统 | Windows 10/11 **x64**（不支持 Win7、ARM64、Linux、macOS） |
| Python | **不需要安装**；运行时已打包在 dist 里 |
| 目录 | 放在**可写**的本地路径，例如 `D:\NEBUMTK\`；不要放 `C:\Program Files`、只读盘、网络共享或 OneDrive 同步目录 |
| USB 驱动 | ADB 需要设备 ADB 驱动；Fastboot 需要 Android Bootloader 驱动；Xingc 需要 WinUSB/libusbK（Zadig） |
| 运行库 | dist 已带 `vcruntime140.dll`/`msvcp140.dll` 等；若仍提示缺 VC++，安装 Microsoft VC++ 2015-2022 x64 |
| 杀毒/SmartScreen | exe 未签名，可能被拦；确认来源后允许运行，或加排除 |
| 架构 | 只支持 64 位 Windows；exe 不能在 32 位系统跑 |

### 复制方法

```powershell
# 源机器
Copy-Item -Recurse dist\nebumtk_unlock_tools.dist D:\NEBUMTK\

# 目标机器直接运行
D:\NEBUMTK\nebumtk_unlock_tools.dist\NEBUMTK_UNLOCK_TOOLS.exe
```

整个目录约 109 MB。第一次运行会在 exe 旁边创建 `workspace\`（日志/临时文件），
所以目录必须可写。

首次运行（对应 Windows 用户还没有 `%APPDATA%\NEBUMTK\settings.json`）默认开启
**测试模式**，顶部会显示“机型模板 / 运行模板”工具条。不需要时在
“工具与设置 → 设置 → 维护测试 → 测试模式”里关闭；已有 settings.json 的机器
保留原来的选择。

### 会跟着走的东西

- `material\xingc\`：**包含签名私钥**。复制这个文件夹等于把解锁能力一起复制走；
  不要发给不可信的人。
- `images\lk.img` / `lk_xingc.img`：只对应 PD2417 的原厂 LK，不是通用镜像。
- `platform-tools\`：adb/fastboot；但 **OEM USB 驱动不在里面**，需要在目标电脑
  安装设备驱动。
- `settings.json` 不在 dist 里，而是每个用户在 `%APPDATA%\NEBUMTK\` 下；新电脑
  首次运行是默认设置。

---

## 1. 目录说明

```text
nebumtk_unlock_tools.dist\
  NEBUMTK_UNLOCK_TOOLS.exe
  README-UNLOCK.txt
  UNLOCK-GUIDE.md              # 本文件
  platform-tools\              # adb.exe / fastboot.exe
  misc\                        # 品牌 misc
  material\xingc\              # catalog + 私钥 + AVB 公钥 + permit
  images\
    lk.img                     # 原厂 LK（恢复点）
    lk_xingc.img               # 已打 Xingc 补丁的 LK
    lk_xingc.img.receipt.json  # 补丁凭据
```

---

## 2. 前置检查（只读，不改设备）

### 2.1 确认型号/平台

```powershell
adb devices
adb shell getprop ro.product.brand
adb shell getprop ro.product.model
adb shell getprop ro.product.device
adb shell getprop ro.board.platform
adb shell getprop ro.build.display.id
adb shell getprop ro.boot.slot_suffix
```

期望：`vivo` / `PD2417` 或 `V2417A` / `mt6989` / `_a` 或 `_b`。

### 2.2 确认设备当前 LK 和 `images\lk.img` 一致

用工具自己的备份功能最稳：

1. 启动 exe；
2. “分区与备份” → 刷新清单（需要 root）；
3. 勾选 `lk` → 备份所选；
4. 打开桌面备份目录里的 `backup-manifest.json`，看 `partitions.lk.sha256`。

应等于：

```text
30927f0fd252c460172d8df75b893b2f570cbc6ccf4f76751c9ae4abc05229dc
```

不一致就不要用现成的 `lk_xingc.img`（见第 7 节）。

### 2.3 确认 root 可持续

```powershell
adb shell su -c id
adb shell su -c id

adb shell su -c "blockdev --getsize64 /dev/block/by-name/misc"
adb shell su -c "blockdev --getsize64 /dev/block/by-name/lk_b"
```

- 两次 `id` 都必须是 `uid=0`；一次性 root 会在写入中途失败。
- `blockdev` 能返回数字，说明 root 能跑实际写入用的命令。
- `lk_b` 容量必须 ≥ **3,821,568** 字节。

### 2.4 其他

- 只连一台设备；其他 adb/fastboot 设备先拔掉。
- 电量 > 50%；用后置 USB 口，不要 hub。
- **解锁会清数据**：先备份照片/文件。
- 关闭其他会占用 adb 的工具（手机助手、scrcpy、Android Studio 等）。

---

## 3. 推荐流程：一键“开始 Vivo 完整解锁”

这条路会用设备自己备份出来的 LK 现场打补丁，比直接用现成 `lk_xingc.img`
多一层“当前 LK 是否匹配”的保护，并且收尾会切回原槽。

1. 启动 `NEBUMTK_UNLOCK_TOOLS.exe`。
2. 设备页等待扫描完成，选中 `iQOO Z9 Turbo+ (PD2417)` / `vivopd2417`。
3. 进“Root 与解锁”。
4. **把 LK 文件字段全部留空**（`source LK`、`output LK`、`patched LK` 都不要选）。
   - 一键流程只使用本次设备备份并自动修补；字段非空会被拒绝。
5. 点 **“开始 Vivo 完整解锁”**。
6. 在破坏性确认框核对：
   - 设备序列号；
   - 目标包含 `inactive LK / patched LK / misc`；
   - 会清数据。
7. 确认后流程会自动：
   - 确认现有 root；
   - 备份 `lk` / `init_boot` / `vendor_boot` 到桌面（失败会停止，不写设备）；
   - 用 `material\xingc` 给本次备份的 LK 打补丁；
   - 写对槽 `lk` + `misc`；
   - 重启到 fastbootd → bootloader Fastboot；
   - Xingc 解锁；
   - 写双槽、切回原槽、重启；
   - 等 Android 启动并确认槽位。
8. **dd 过程中不要拔线、不要 Ctrl-C**。

如果它在“内部 UBL 预检”失败（`not-found` / `conflict`），说明设备当前 LK 和
`material\xingc\patch-catalog.json` 不匹配；不要强行继续，按第 7 节重新生成。

---

## 4. 手动分步流程（备用）

想用已经打好的 `images\lk_xingc.img` 时：

1. 启动 exe，扫描并选中 PD2417。
2. “Root 与解锁” → LK 文件 → 选择 `images\lk_xingc.img`。
3. 点 **“准备解锁”**。
   - 写对槽 `lk` + `misc`，然后重启到 bootloader Fastboot。
4. 设备进入 **bootloader Fastboot** 后，点 **“Xingc 授权 / 解锁”**。
5. 解锁成功后，点 **“同步双槽”**。
   - 写 `lk_a` + `lk_b`，然后重启。
   - 手动“同步双槽”可能停留在当前 patched 槽，不一定显式切回原槽；以界面日志
     里的实际槽位为准。

手动路径前提：

- 设备当前 LK 哈希必须等于 `30927f0f...`；
- `images\lk_xingc.img` 与 `material\xingc` 必须成对；
- 不要把这个 LK 用到别的机型/固件。

---

## 5. 成功后确认

```powershell
adb devices
adb shell getprop sys.boot_completed
adb shell getprop ro.boot.slot_suffix
```

- `sys.boot_completed=1` 表示系统已启动。
- 确认 bootloader 解锁状态：重启到 bootloader 后
  ```powershell
  fastboot getvar unlocked
  ```
  或在应用里看 Xingc 解锁后的状态。

数据被清空是正常的；重新设置系统即可。解锁后 root 可能丢失，本构建不负责
重新 root。

---

## 6. 常见失败与处理

| 现象 | 处理 |
|---|---|
| “设备未 root；此构建不执行提权” | 先自己取得 root；确认两次 `adb shell su -c id` 都是 uid=0 |
| `No backend available` / libusb 报错 | 用 Zadig 把 Fastboot 接口绑定到 WinUSB/libusbK；确认 dist 里有 `libusb-1.0.dll` |
| 内部 UBL 预检 `not-found` / `conflict` | 设备 LK 与 catalog 不匹配；重新生成 catalog/patched LK，不要强刷 |
| 准备解锁后进了 Android，而不是 Fastboot | misc/槽位不对；不要继续点 Xingc，先检查 `ro.boot.slot_suffix` 和 misc 写入日志 |
| 写入过程中 dd 报错 | 不要手动重启；记录错误。对槽可能只写了一半，设备通常还能从原槽启动；用 `images\lk.img` 恢复对槽后再试 |
| 开机 bootloop | 用 `images\lk.img` 写回对槽（fastboot 或 BROM），恢复 misc；不要反复刷 |
| 同步双槽时 fastboot 看不到设备 | 驱动被改成了 libusbK/WinUSB；恢复标准 Android Bootloader 驱动，或换 USB 口/线 |
| 只复制了 exe 打不开 | 必须复制整个 `nebumtk_unlock_tools.dist` 目录 |
| 提示缺 VC++ 运行库 | 安装 Microsoft VC++ 2015-2022 x64 |
| 杀毒/SmartScreen 拦截 | 确认来源后允许；或加排除 |

---

## 7. 设备不是 PD2417 / LK 哈希不一致

**不要用现成的 `images\lk_xingc.img`。** 需要针对当前设备的原厂 LK 重新生成：

```powershell
python tools\prepare_xingc_lk.py --lk <设备当前-lk.img> --dry-run
```

- 识别出 `unlock2` / `non_unlock2` 和三个补丁点 → 可以：
  ```powershell
  python tools\prepare_xingc_lk.py --lk <设备当前-lk.img>
  ```
  然后把新的 `material\xingc`、`lk_xingc.img` 放到 dist 对应位置。
  一键流程用的 catalog 也要换成新生成的这份。
- 报 `no known Xingc command context` → 这份 LK 布局当前工具不支持，停。
- OPPO / Honor → 流程不同，不能用这套 Vivo Xingc 素材。

> `tools\prepare_xingc_lk.py` 需要仓库里的 Python；目标电脑上如果没有源码，
> 就只能用已经匹配的 dist 素材。

---

## 8. USB 驱动

- **ADB**：设备正常连 Windows，装 vivo/Google USB 驱动，`adb devices` 能看到。
- **Fastboot**：设备进 bootloader，Windows 识别为 Android Bootloader Interface；
  `fastboot devices` 能看到。
- **Xingc**：应用走 PyUSB/libusb，需要 Fastboot 接口绑定到 **WinUSB/libusbK**。
  可用 Zadig：Options → List All Devices → 选 MTK Fastboot 设备 → Replace Driver
  → WinUSB（或 libusbK）。
  - 改驱动后如果 `fastboot.exe` 看不到设备，恢复标准 Android Bootloader 驱动，
    或换一个 USB 口/线再试。
- dist 已带 `libusb-1.0.dll`；不需要额外装 libusb。

---

## 9. 安全与边界

- 本构建**不做 payload 提权**，也不安装 SakiSU/Magisk/KernelSU；它只使用现有 root。
- `material\xingc\verify-private.pem` 是签名私钥；复制 dist 等于复制解锁能力。
- `images\lk_xingc.img` 只对应 `images\lk.img` 那份原厂 LK；换机型/固件必须重做。
- 这个 dist 不是可公开分发的 release：带私钥，`audit_distribution` 会拒绝；
  对外分发前把 `material` 移出。
- 只对你拥有或获授权的设备操作；写 bootloader 分区有变砖风险。
- 本构建**没有做真机启动验证**；第一次写建议只写对槽，保留原槽可恢复。
