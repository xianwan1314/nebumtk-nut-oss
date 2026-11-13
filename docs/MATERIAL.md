# 本地素材（material）

开源版没有访问密码，也没有随仓库分发的密文。凡是闭源版用 master key 解出来的东西，
开源版都当成操作者自己的本地文件读。**所有素材目录都在 `.gitignore` 里。**

## 1. 有效路径

解析顺序（`nebumtk_unlock_tools/keymaterial.py`）：

1. 显式传入的 `root=`（工具与测试）；
2. 环境变量 `NUT_MATERIAL_ROOT`；
3. `<项目根>/material`（默认）。

## 2. 目录布局

```text
material/
  wrap-private.pem             # RSA-3072 私钥：解封每个 payload 的 CEK（OAEP-SHA256）
  preload-pack.key             # 32 字节：机型包内 payload CEK 的对称密钥（hex/base64/raw 皆可）
  payloads/
    manifest.json              # 每个 payload 的 wrapper 哈希、OAEP label、被包裹的 CEK
    <payload_id>/wrapper.so    # 推到设备上的加密 wrapper（ELF，hash 固定）
  xingc/
    protocol-permit            # 内容为 NEBUMTK-XINGC-PROTOCOL-PERMIT-v1，标记协议许可
    verify-private.pem         # Xingc 签名私钥
    verify-public.avbpub       # 与私钥配套的 AVB public key blob（520 字节）
    patch-catalog.json         # 已审核的 LK 补丁目录
  sakisu/                      # 上游项目 SakiSU 的产物（见第 2.1 节）
    official-patcher-arm64
    official-patcher-manifest.json
    official-patcher-windows-x64.exe
    official-patcher-windows-x64-manifest.json
```

### 2.1 SakiSU（上游项目，不分发）

SakiSU 不是本仓库的一部分：<https://github.com/XingChenRS/SakiSU>，引用版本
`v4.3.0-sakisu.1`。仓库里只保留我们自己的哈希清单（`sakisu/*manifest.json`），二进制
由 `tools/fetch_sakisu.py` 拉进 `material/sakisu/`：

```powershell
# 从上游 release 下载官方 APK 并解出 arm64 patcher
python tools\fetch_sakisu.py --download
# 或者交给它本地已有的文件
python tools\fetch_sakisu.py --apk SakiSU_v4.3.0-sakisu.1_35039-arm64-v8a-release.apk --host ksud.exe
# 校验现有 store（打印 size + SHA-256 + 上游 tag）
python tools\fetch_sakisu.py --check
```

也可以用 `NUT_SAKISU_HOME` 指向一个现成的 SakiSU 目录。查找顺序：
`NUT_SAKISU_HOME` → `<项目根>/sakisu`（打包产物会把 store 放在 exe 旁边）→
`<material>/sakisu`。缺产物时流程以 `VerificationError` 失败，消息里带上游链接。

`material/payloads/manifest.json` 的形状：

```json
{
  "version": 1,
  "native_build_sha256": "<wrapper 源码与 NDK 组合的指纹>",
  "payloads": {
    "<payload_id>": {
      "plain_sha256": "<明文 payload 的 SHA-256>",
      "wrapper_sha256": "<wrapper.so 的 SHA-256>",
      "oaep_label": "<base64(NEBUMTK-WRAP-v1:<id>:arm64-v8a:35)>",
      "wrapped_key": "<base64(RSA-OAEP(CEK))>"
    }
  }
}
```

## 3. 生成步骤

```powershell
# 1) 明文 payload 放进 preloads/<payload_id>/preload.so，然后编译 wrapper
python tools\embed_preloads.py --regenerate
#    首次运行会生成 material/wrap-private.pem（3072 位 RSA），之后一直复用
#    校验已有 store：
python tools\embed_preloads.py

# 2) Xingc 材料（derive 自 xingc_sources/）
python tools\import_xingc_material.py
python tools\import_xingc_material.py --check

# 3) 机型包密钥（只有需要 catalog 内嵌 preload 时才用）
python tools\build_device_catalog.py --init-key
python tools\build_device_catalog.py --regenerate
```

`--regenerate` 需要 Android NDK 29.0.14206865（`ANDROID_NDK_HOME` 或
`%LOCALAPPDATA%\Android\Sdk\ndk\...`）。

如果手头只有一份官方 LK 镜像、没有 `xingc_sources/`，可以用
`tools/prepare_xingc_lk.py` 从镜像本身推导三个 Xingc 补丁点，生成
`material/xingc/` 的目录、RSA-2048 密钥对和许可标记，并产出已验证的 patched LK。
它只读写本地文件，不连接设备：

```powershell
python tools\prepare_xingc_lk.py --lk imags\lk.img --dry-run
python tools\prepare_xingc_lk.py --lk imags\lk.img
python tools\prepare_xingc_lk.py --lk imags\lk.img --check
```

自动识别只接受已知的 Vivo `veify` 布局；其他布局请用 `--catalog` 指定目录文件。
生成的 `verify-private.pem` 是这台工具自己的签名私钥，**备份它，不要外传**；换键要
`--regenerate-key` 并重新生成 patched LK。

## 4. 运行时行为

- 缺 `material/payloads/manifest.json` → 未登记的 payload 报 `KeyError`/`MaterialError`，
  消息里带绝对路径和补法。
- wrapper 哈希不符、不是 ELF → `MaterialError`，不会把文件推给设备。
- 缺 `material/xingc/*` → `MaterialError`，Xingc 相关按钮在流程开始前失败。
- `verify_pair()` 会比对 Xingc 私钥与 AVB public blob，不匹配直接拒绝。

## 5. 安全边界

这些文件在磁盘上就是明文/私钥，**只在操作者自己的机器上有效**。开源版不提供任何
"保护层"：谁拿到 `material/` 谁就拿到同样的能力。这是刻意的取舍——闭源版的
[LICENSING_AND_PROTECTION.md](LICENSING_AND_PROTECTION.md) 已经把结论写清楚：
客户端密码门禁在秘密已公开后收益接近零。
