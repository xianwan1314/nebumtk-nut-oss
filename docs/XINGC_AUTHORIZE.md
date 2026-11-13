# Xingc 授权工具：原理与实现

这份文档解释“自己生成一对密钥 → patch LK → 用这对密钥做授权工具”的完整链路，
以及本仓库里对应的实现。

> 只对你自己拥有或已获授权的设备使用。这是自签自验的授权链，不是厂商服务器授权。

---

## 1. 它到底在做什么

原厂 LK 里有一条 OEM 校验命令（源码/字符串里拼成 `veify`），以及一张 MTK
`seed_auth` 公钥表。解锁校验大致是：

```text
设备返回 token
  → 主机用私钥签名
  → 主机把 token + 签名传给设备
  → 设备用 LK 里的公钥验签
  → 通过则 unlock
```

LK patch 做三件事：

1. 把 `veify` 命令改名为 `xingc`（等长 5 字节）；
2. 把对应日志标记也改成 `xingc`；
3. 把 `hedgehog` seed auth 槽位的 520 字节 AVB **公钥**替换成**你的公钥**；
4. 重建 cert2（新哈希 override 块 + 原签名证书），让设备还能启动。

所以：

- LK 里是**你的公钥**，不是私钥；
- 私钥只在你的电脑上；
- 设备之后只认“用你的私钥签出来的 token”；
- `xingc unlock` / `xingc lock` 就是改名后的原厂校验命令。

---

## 2. 完整链路

```text
1. 生成 RSA-2048 私钥 + 520 字节 AVB 公钥 blob
2. 用 patch catalog 把公钥写进原厂 LK，得到 patched LK
3. 把 patched LK 写进设备（对槽 / 双槽）
4. 设备启动 patched LK，暴露出 xingc 命令
5. 主机 getinfo unlock 拿 token
6. 主机 RSA-2048 PKCS#1 v1.5 + SHA-256 签 token
7. 主机 download token+签名
8. 主机发 xingc unlock
9. 设备用 LK 里的公钥验签 → OKAY
10. 双槽同步，让两个槽都保留 patched LK
```

关键点：**第 1 步和第 2 步必须成对**。换了私钥就要重新 patch LK，把新公钥写进去；
只换授权工具而不重打 LK，设备验签会失败。

---

## 3. 本仓库里对应的实现

| 步骤 | 代码/工具 |
|---|---|
| 生成密钥 + catalog + patched LK | `tools/prepare_xingc_lk.py` |
| LK patch 引擎 | `nebumtk_unlock_tools/lk/ubl/*`、`lk/ubl_service.py` |
| LK 写入/双槽同步 | `flows/stages.py`、GUI 的“准备解锁 / 同步双槽” |
| token 读取、签名、upload、unlock/lock | `nebumtk_unlock_tools/lk/xingc_service.py` |
| 独立授权 CLI | `tools/xingc_authorize.py` |
| GUI 授权入口 | “Root 与解锁 → Xingc 授权 / 解锁” |

### 3.1 生成密钥与 patched LK

```powershell
python tools\prepare_xingc_lk.py --lk imags\lk.img --dry-run
python tools\prepare_xingc_lk.py --lk imags\lk.img
python tools\prepare_xingc_lk.py --lk imags\lk.img --check
```

生成：

```text
material/xingc/verify-private.pem      # 私钥，留在电脑
material/xingc/verify-public.avbpub    # 520 字节 AVB 公钥 blob
material/xingc/patch-catalog.json
material/xingc/protocol-permit
imags/lk_xingc.img
```

如果已有密钥、只想换 LK，可以复用私钥；只有需要换密钥时才
`--regenerate-key` 并重打 LK。

### 3.2 独立授权 CLI

```powershell
# 只检查密钥、permit、libusb 后端
python tools\xingc_authorize.py --check

# 读取设备 token
python tools\xingc_authorize.py --serial <SERIAL> --token

# 只签名，打印 token+签名（给外部工具用）
python tools\xingc_authorize.py --serial <SERIAL> --sign <TOKEN>

# 完整授权解锁
python tools\xingc_authorize.py --serial <SERIAL> --unlock

# 回锁
python tools\xingc_authorize.py --serial <SERIAL> --lock
```

`--unlock` 内部就是：

```text
XingcUnlockService.unlock(serial, fastboot)
  token = protocol.get_unlock_token()          # getinfo unlock
  payload = token + hex(rsa_sign(token))       # RSA-2048 PKCS#1 v1.5 SHA-256
  protocol.download(payload)                   # download:<len>
  protocol.command("xingc unlock")             # 设备验签
  fastboot.is_unlocked()                       # 状态确认
```

### 3.3 自己实现的最小代码

如果不想用 CLI，直接调用 service：

```python
from nebumtk_unlock_tools import keymaterial
from nebumtk_unlock_tools.lk.xingc_service import XingcUnlockService
from nebumtk_unlock_tools.core.env import EnvChecker
from nebumtk_unlock_tools.core.fastboot import FastbootClient
from nebumtk_unlock_tools.config.store import SettingsStore
from pathlib import Path

keymaterial.set_project_root(Path(".").resolve())
settings = SettingsStore().load()
tools = EnvChecker(settings).validate(need_fastboot=True)
fastboot = FastbootClient(tools.fastboot, serial="<SERIAL>")
service = XingcUnlockService(log=print)
service.unlock("<SERIAL>", fastboot)
```

前提：

- 设备当前运行的是**内嵌你公钥的 patched LK**；
- `material/xingc` 与那份 LK 成对；
- 设备在 bootloader Fastboot，且 Fastboot 接口绑定 WinUSB/libusbK；
- 已安装 PyUSB/libusb：`pip install pyusb==1.3.1 libusb==1.0.29`。

---

## 4. “双槽同步”为什么重要

- 只写对槽：设备切回原槽后，`xingc` 命令和你的公钥就不在了；
- 双槽同步：把 patched LK 同时写入 `lk_a` 和 `lk_b`，之后无论设备在哪个槽，
  都能用你的密钥授权；
- 一键 Vivo 完整解锁的收尾会写双槽并切回原槽；手动“同步双槽”也会写双槽，
  但可能停在当前 patched 槽。

---

## 5. 边界与风险

- 这是**自签自验**：设备只认你 patch 进去的公钥；不需要厂商 Xingc 私钥。
- 私钥丢了：设备已解锁不会立刻变回锁定，但你无法再为这份 patched LK 签新 token；
  换密钥必须重打 LK 并重新写入。
- 换官方固件/官方 LK：你的公钥和 `xingc` 命令会被覆盖，需要针对新 LK 重新 patch。
- anti-rollback：新固件可能拒绝旧 patched LK；不要混刷。
- 本工具不提供 root、不提供 payload、不绕过设备上的其他安全边界；只做 token 授权。
- 只对你拥有或获授权的设备使用。
