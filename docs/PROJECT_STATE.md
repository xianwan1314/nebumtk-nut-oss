# NEBUMTK Offline Utility — Project State

Compact authoritative handoff. Replace stale facts rather than appending a log.
Last reconciled: 2026-11-04 (open-source conversion).

## Status

`main` 是 **2.0 开源基线**。它是闭源 2.0（tag `baseline2-sealed`）去掉访问密码门禁、
去掉 VMProtect 版本；产品行为、设备侧协议与 UI 拓扑未改。

另一个 baseline 在 `baseline/1.1`（tag `v1.1.0`），对应最后实际发货的离线 1.1。

## Personal no-escalation build

当前工作区还包含一个**个人构建**（不是默认发行形态）：

- 运行时不做 payload 提权：`ensure_root()` 只探测已有 `su`（连续两次 `uid=0`），
  `escalate_root()` 直接报错；不再推送 preload。
- UI 隐藏“临时 Root / 获取临时权限”和 SakiSU root 安装；一键 Vivo 只使用现有 root。
- SakiSU 上游二进制未获取，镜像页的 SakiSU 页签隐藏；代码保留但不可达。
- 机型目录已用 `local-dev/` 的本地密钥重签，仍有 23 个档案。
- 构建步骤与踩坑记录见 [PERSONAL_BUILD.md](PERSONAL_BUILD.md)；产物在
  `dist/nebumtk_unlock_tools.dist/`，`material/xingc` 与 `images` 随包放在 exe 旁边。
- 个人构建的 `scripts/build.ps1` 不再调用 audit；`tools/audit_distribution.py`
  保留给公开发布时手动执行。
- 新 profile（没有 `settings.json`）默认开启测试模式，顶部显示机型/运行模板；
  已有 settings.json 保留原值，可在设置里关闭。
- 解锁操作、复制到其他电脑、失败处理见 [UNLOCK-GUIDE.md](UNLOCK-GUIDE.md)；
  dist 里也带有 `UNLOCK-GUIDE.md`。
- 该目录含签名私钥，**不是可公开分发的 release**；对外分发前必须把
  `material/xingc` 移出，或换成不随包分发的素材。

## Licensing

代码以 **AGPL-3.0-only** 发布（`LICENSE`，全文取自 FSF/SPDX 原文）；第三方组件的许可以
及 Qt 只能动态链接的约束见 `NOTICE.md`。这条许可不随分支变化，1.1 与 2.0 相同。

## What this is

一个纯本地的 Windows 产品：MediaTek v6 bootloader 解锁 + 通用分区备份/还原 + 品牌流程，
单 Python/PySide6 应用，Nuitka standalone 分发。无服务端、无账号、无网络、无访问密码。

## Architecture

```text
nebumtk_unlock_tools.__main__ -> app.main()
  -> gui.main_window.MainWindow
  -> QThread workers
  -> flows.factory.create_flow()  (flow_kind -> a typed flow)
  -> FlowContext + AdbClient/FastbootClient + ResourceLoader + OperationFiles
```

- 五页 UI：设备 / 分区与备份 / Root 与解锁 / 镜像与恢复 / 工具与设置。
- 中心契约：`core/operation_files`（不可变输入 + 操作私有输出）、`core/safety`
  （终局复验）、`core/slot`（活动/非活动配对）、`resources/loader`（preload 物化）、
  `lk/ubl_service` + `lk/xingc_service`（LK 补丁与 Xingc 授权）。
- `keymaterial`（新）= 素材查找层：显式 root → `NUT_MATERIAL_ROOT` → `<项目>/material`。
- `resources/payload_store`（新）= preload 库：`manifest.json` + `<id>/wrapper.so` +
  `wrap-private.pem`，取代原来的 `resources/embedded_preloads.py` 密文模块。
- `lk/keys.py` 直接读 `material/xingc/*` 明文文件，取代 `lk/embedded_xingc.py`。
- 产品通道固定在 `build_profile`（`BUILD_CHANNEL = "offline"`，
  `CLIENT_VERSION = "2.0.0"`）。

## Product boundaries

- 持久 Root 管理不在范围内。唯一获批的 root 相邻面是 SakiSU 4.3 的 `init_boot` 镜像
  处理与可选的 Vivo 原槽写入；没有管理器 APK、root 控制台、KMI 设备查询、
  `vendor_boot` 补丁或双槽 `init_boot` 刷写。
- 临时提权（受保护 preload + 一次性 `su`）是备份与 system 侧分区写入的内部前提。
- 测试模式只放松兼容性匹配；精确设备、传输状态、槽位、容量、冻结哈希、取消、终局
  复验与 Vivo 完整流程永不放松。
- 仓库不携带厂商素材（LK、payload、Xingc 私钥、固件），也不携带任何密码或壳。

## Verified state (2026-11-04)

```text
python -m pytest -q  ->  594 passed, 19 skipped      (nut-oss main)
python -m pytest -q  ->  623 passed,  5 skipped      (baseline2-sealed，转换前对照)
```

19 个 skip 全是"操作者素材缺失"（payload `.so`、`xingc_sources/`、`.private/` 里的
审阅构建）。删除 15 个密码/壳用例、新增 8 个素材层用例后，计数与 623 对得上。

已核对：

- `MainWindow` 在无密码输入的情况下可构造（`QT_QPA_PLATFORM=offscreen` 冒烟）。
- `resolve_offline_preload()` 在没有 payload store 时报带路径的 `KeyError`/`MaterialError`。
- `tools/audit_distribution.py` 仍拒绝 `.vmp`/`.map`/`.pdb`/`*.vmp.exe`、明文 payload、
  可编辑目录。

未执行（需要 Windows 打包环境 / 真机）：

- Nuitka standalone 构建与发行隔离审计。
- SFX 打包与解包复验。
- 物理设备端到端验收（Vivo 完整解锁、OPPO 解锁、通用备份/还原、SakiSU 单槽试跑）。

## Release pipeline (per candidate)

```powershell
python -m pytest -q tests --basetemp=<project-local-temp>
.\scripts\clean.ps1
.\scripts\build.ps1 -Channel Offline
python tools\audit_distribution.py dist\nebumtk_unlock_tools.dist
.\scripts\package-sfx.ps1        # 可选
```

构建链里没有壳阶段：Nuitka 之后直接审计、直接 SFX。旧 `dist/`、旧哈希或过去的 smoke
结果不代表当前源码已发布，每个候选都要记新的输出。

## Known risks

- 2026-07 泄漏事件按完全公开处理：曾出现在闭源树里的密钥与 payload 材料不能再当秘密。
  开源版因此不携带它们，也不再假装客户端能守住它们。
- `misc/` 里的品牌 bootloader 镜像随仓库分发（owner 决定保留；厂商内容见
  `NOTICE.md`）。
- `sakisu/` 只保留我们的哈希清单，SakiSU 的二进制由操作者用
  `tools/fetch_sakisu.py` 拉进 `material/sakisu/`。本机已按该布局放好，测试会用到。
- Honor reserve-LK 释放与 SakiSU 单槽 boot 试跑仍未做真机验收。

## Next decisions

- 真机验收排期。
