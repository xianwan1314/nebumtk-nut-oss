NEBUMTK UNLOCK TOOLS — 个人构建说明（无提权版）

详细的操作步骤、前置检查、失败处理见同目录的 UNLOCK-GUIDE.md。

本构建与仓库默认行为的两点差异：
1. 不包含任何提权/root 安装链路：不会推送 payload，不会调用 RootEscalator，
   也不会安装 SakiSU/Magisk/KernelSU。程序假定设备上已经存在可用的 su。
2. SakiSU 上游二进制未打入：镜像页只显示 Vivo Dyn 与 LK 工具；SakiSU 页签已隐藏。

目录说明
  NEBUMTK_UNLOCK_TOOLS.exe   主程序
  material\xingc\            Xingc 素材（含私钥，务必自行备份，不要外传）
  images\lk.img              原厂 LK（未改动）
  images\lk_xingc.img        已打好 Xingc 补丁的 LK
  platform-tools\            adb / fastboot
  misc\                      品牌 misc 镜像

自检
  NEBUMTK_UNLOCK_TOOLS.exe --self-check
  结果会打印并写入同目录的 self-check.txt；全部 [ok] 才表示
  机型签名、Xingc 密钥对、patched LK、Xingc 后端和 platform-tools 都正常。

使用前
  1. 首次运行（还没有 settings.json）默认开启测试模式，顶部会显示
     “机型模板 / 运行模板”；不需要可在“工具与设置 → 设置”里关闭。
  2. 设备连 ADB，确认：
         adb shell su -c id
         adb shell su -c id
     两次都必须是 uid=0。一次性 root 不够，写入过程中会多次调用 su。
  3. Xingc 解锁使用 PyUSB/libusb，需要设备在 Fastboot 下被识别为
     WinUSB/libusbK 设备（可用 Zadig 绑定驱动）。
  4. 本构建不替你取得 root。设备没有 root 时，“准备解锁”会以明确错误停止，
     不会尝试提权。

推荐流程（Vivo）
  1. 启动 exe，扫描设备，选中对应机型（PD2417 / iQOO Z9 Turbo+）。
  2. 打开“Root 与解锁”，在 LK 文件里选择 images\lk_xingc.img。
  3. 点“准备解锁”：写对槽 lk + misc，然后重启到 bootloader Fastboot。
  4. 点“Xingc 解锁”：用 material\xingc 的私钥签 token。
  5. 点“同步双槽”：写 lk_a + lk_b，切回原槽并重启。
  6. 确认开机、确认 fastboot getvar unlocked。

注意
  - images\lk_xingc.img 只对应这台 PD2417 的原厂 LK；不要用于其他机型或固件。
  - 写 LK/misc 的 dd 过程不要拔线、不要 Ctrl-C。
  - 本流程不备份当前 LK；请保留 images\lk.img 作为恢复点。
  - material\xingc\verify-private.pem 是签名私钥：丢失后需要换密钥并重新打 LK。
  - 本构建为了方便使用把私钥放在了程序目录，因此不适合公开分发；
    对外分发前请把 material 移出，或重新生成一套不随包分发的素材。
