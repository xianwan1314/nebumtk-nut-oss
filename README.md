# NEBUMTK Unlock Tools — open source

MediaTek v6 bootloader unlock and local partition work for Windows, as a single
Python/PySide6 desktop application. Fully local: no server, no account, no
network, no access password.

This repository is the **open-source release** of the NUT tool. It carries two
baselines of the same product:

| baseline | branch | tag | 形态 |
|---|---|---|---|
| 1.1 | `baseline/1.1` | `v1.1.0` | 最后实际发货的离线版（五页 UI 的第一版） |
| 2.0 | `main` | `v2.0.0` | 重构后的离线版（typed contracts、SakiSU 主机侧补丁边界、通用分区备份） |

两个 baseline 是同一仓库的两个孤儿分支，可以直接互相 diff：

```powershell
git log --oneline baseline/1.1
git diff baseline/1.1..main -- nebumtk_unlock_tools
# 或者并排工作树
git worktree add ..\nut-1.1 baseline/1.1
```

## 与闭源发行版的差异

开源处理只删两类东西，产品行为不变：

1. **访问密码门禁**（`security.py`）：闭源版用 PBKDF2 从操作者输入的访问密码派生
   master key，再用它解内置的 preload 密文、Xingc 密钥与机型包密钥。开源版没有这个
   门禁，也没有任何随仓库分发的密文——受保护材料改成操作者自己的本地文件
   （见 [docs/MATERIAL.md](docs/MATERIAL.md)）。缺文件时报 `MaterialError` 并给出
   绝对路径，不再表现为"密码错了"。
2. **VMProtect 壳**（`vmp/`、`scripts/protect-*.ps1`、`tools/*vmp*`）：开源版不带
   壳、不带 receipt 校验，SFX 也不再加压缩密码。`tools/audit_distribution.py` 仍然
   拒绝 `.vmp`/`.map`/`.pdb`/`*.vmp.exe` 这类中间物进发行目录——那是卫生规则，不是
   构建依赖。

设备侧协议（加密 wrapper、X25519 challenge、HKDF-SHA256 + AES-GCM grant、匿名 fd
执行）**一字未改**，所以行为与 1.1/2.0 完全一致。

## 代码结构

```text
nebumtk_unlock_tools.__main__ -> app.main()
  -> gui.main_window.MainWindow      (五页：设备 / 分区与备份 / Root 与解锁 / 镜像与恢复 / 工具与设置)
  -> QThread workers
  -> flows.factory.create_flow()     (flow_kind -> typed flow)
  -> FlowContext + AdbClient/FastbootClient + ResourceLoader + OperationFiles
```

- `nebumtk_unlock_tools/` 是产品本体，`tests/` 是可执行契约。
- `core/operation_files`（不可变输入与操作私有输出）、`core/safety`（终局复验）、
  `core/slot`（活动/非活动槽配对）、`resources/loader`（preload 物化）、
  `lk/ubl_service` + `lk/xingc_service`（LK 补丁与 Xingc 授权）是关键契约层。
- `native/preload_wrapper/` 是设备侧 C wrapper 源码（monocypher + pycryptodome 的
  AES-GCM/SHA-256 实现，见 [NOTICE.md](NOTICE.md)）。
- `tools/` 是构建与校验工具，`scripts/` 是构建与打包入口。

## 快速开始（开发机）

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install pytest==9.0.2
$env:QT_QPA_PLATFORM = "offscreen"
.\.venv\Scripts\python.exe -m pytest -q --basetemp=.pytest-basetemp
.\.venv\Scripts\python.exe -m nebumtk_unlock_tools
```

测试里有 19 个用例依赖操作者私有素材（payload `.so`、Xingc 目录、审阅过的静态
构建），缺素材时 skip 而不是失败——这是刻意的，仓库不携带这些素材。

## 素材要求

仓库里**没有任何秘密**，也没有可直接上设备的 payload。要真正跑品牌流程，需要自己
准备 `material/`（密钥与 payload store）、`preloads/`（明文 payload 源）、
`xingc_sources/`（Xingc 密钥与补丁目录）。目录布局与生成命令见
[docs/MATERIAL.md](docs/MATERIAL.md)。

不带素材时程序照常启动，设备扫描与分区浏览可用，只有需要 payload 的解锁/提权流程会
以 `MaterialError` 明确失败。

## 构建

```powershell
.\scripts\build.ps1 -Channel Offline      # Nuitka standalone -> dist/nebumtk_unlock_tools.dist
python tools\audit_distribution.py dist\nebumtk_unlock_tools.dist
.\scripts\package-sfx.ps1                 # 可选：打成自解压 exe
```

> 本工作区的个人构建已从 `scripts/build.ps1` 去掉 audit 调用，允许把
> `material/xingc` 放在 exe 旁边；上面第 2 行是公开发布时才需要手动执行的审计。

`scripts/build.ps1` 在 `material/payloads/manifest.json` 存在时会顺带校验 payload
store；没有 store 就跳过并给出黄色提示。

本工作区的“无提权、假设已有 root、把 Xingc 素材放在 exe 旁边”的个人构建步骤，
以及本机打包时踩过的 Nuitka/缓存/网络坑，见
[docs/PERSONAL_BUILD.md](docs/PERSONAL_BUILD.md)。

解锁操作步骤、复制到其他电脑的注意事项与失败处理见
[docs/UNLOCK-GUIDE.md](docs/UNLOCK-GUIDE.md)。

Xingc 授权链（自己的密钥、patch 后的 LK、token 签名与独立 CLI）见
[docs/XINGC_AUTHORIZE.md](docs/XINGC_AUTHORIZE.md)。

私有仓库的完整归档（含 `local-dev`、`material`、LK 镜像）提交清单见
[docs/PRIVATE_REPO.md](docs/PRIVATE_REPO.md)。

## 边界

- 本仓库不含 LK 镜像、机型固件、厂商 bootloader 补丁或随包 payload；这些都由操作者
  在运行时选择或自行准备。
- 不提供持久 Root 管理（无 KernelSU/SakiSU/Magisk 管理器、无 `libksud.so`、无 root
  控制台）。唯一例外是 `init_boot` 的 SakiSU 镜像补丁边界和可选的 Vivo 原槽写入。
- 分发目录审计禁止明文 payload、Xingc 私密材料、调试符号与壳中间物。
- 工具只应作用于你自己拥有或已获授权的设备。

## 上游项目：SakiSU

SakiSU 是另一个独立项目：<https://github.com/XingChenRS/SakiSU>（引用
`v4.3.0-sakisu.1`）。本仓库**不再分发它的二进制**，只保留自己的哈希清单
`sakisu/*manifest.json` 作为索引，产物由操作者拉取：

```powershell
python tools\fetch_sakisu.py --download          # 官方 APK -> material/sakisu
python tools\fetch_sakisu.py --apk <apk> --host <ksud.exe>
python tools\fetch_sakisu.py --check
```

## 署名与溯源

```
Copyright (c) 2026 雾月星辰 & MLXC <github@XingChenRS>
SPDX-License-Identifier: AGPL-3.0-only
Provenance token: NBMTK-canary-7f3a91c4e8b2d05f
```

每个第一方源文件带这两行 SPDX 头；`LICENSE` 开头是同一份署名；`--license` 会把它打印
出来。删掉这些声明的发行版不是合法副本（AGPL §5），抄走的代码里仍会带着 token——
`python tools\audit_provenance.py --scan <path>` 可以把证据列出来。机制详见
[docs/PROVENANCE.md](docs/PROVENANCE.md)。

## 许可

本项目代码以 **GNU Affero General Public License v3.0（only）** 发布——强著佐权，不是
宽松许可。全文见 [LICENSE](LICENSE)；署名与溯源标记见 [COPYRIGHT](COPYRIGHT)。

对使用者的实际含义：

- **分发即传染。** 你把修改版（或原版）编译后发给别人，就必须一并提供完整对应源码，
  并以同一许可授权；不能把本项目的代码并进闭源产品。
- **网络使用同样触发（AGPL §13）。** 如果把它包装成在线服务（哪怕只是在服务器上跑一个
  API 再对外提供），使用者必须向服务对象提供源码。
- **没有额外限制的再许可。** 不能用 MIT/Apache 之类的条款重新发布本项目代码；衍生作品
  只能继续 AGPL-3.0-only（`-only`，不接受"或更高版本"的自动升级）。
- 专利授权（§11）与终止（§8）随之生效。

第三方组件（Monocypher、PyCryptodome、Qt/PySide6、Android platform-tools、厂商 misc
镜像、上游 SakiSU）仍按各自的许可，详见 [NOTICE.md](NOTICE.md)。其中 Qt/PySide6 是
LGPL-3，本项目只做动态链接、不做静态链接，分发时必须保留其许可与可替换性。
