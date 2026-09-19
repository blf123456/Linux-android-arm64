# 云编译与作者构建流程对齐

## 已确认的差异

对照基准为 `lsnbm/Linux-android-arm64` 提交
`024251cf38ff8b142d6efbf58a9df6cd751e19f9` 的 `build_all.sh` 和
`install_driver.sh`。基准模块从脚本的 Base64 数据直接解码，没有执行安装脚本。
旧云端产物来自 Actions `35438339077`，驱动提交为
`a840cc263fd5483c54e17f97f1840272c20b11b9`。

| 检查项 | 作者产物 | 修复前云端产物 |
| --- | --- | --- |
| 6.1 `.gnu.linkonce.this_module` 大小 | 1088 字节 | 1024 字节 |
| 6.18 `.gnu.linkonce.this_module` 大小 | 1664 字节 | 1600 字节 |
| 6.12 / 6.18 `init_module` 前的 KCFI 类型 ID | `0x6fbb3035` | `0x36b1c5a6` |
| 6.1 内核版本 | 6.1.159 | 6.1.176 |
| 6.6 内核版本 | 6.6.119 | 6.6.142 |
| 6.12 内核版本 | 6.12.23 | 6.12.92 |
| 5.15 内核版本 | 5.15.197 | 5.15.211 |
| 两个 5.10 分支内核版本 | 5.10.247 | 5.10.264 |

工具链版本、ARM64 架构和空符号 CRC 策略本身相同，但这不足以证明内核 ABI 相同。
模块结构大小和 KCFI 类型标识会影响内核加载/调用模块。
没有失败设备的内核日志，不能断言每个人的失败都由同一个差异导致。

旧云端只下载 `common` 和 Clang，执行 `gki_defconfig`，再自行修改 Rust、BTF 和
modversions 配置。它绕过了作者的 Bazel/Legacy 配置、生成文件及构建工具环境。

## 修复后的流程

1. 同步 Android 官方 kernel manifest 的完整构建依赖，并保存 `repo manifest -r`。
2. 本地和云端共用 `build_selected_kernel`：Android 13+ 调用作者的 `build_kernel`，
   Android 12 调用作者的 `build_legacy_kernel`。
3. Bazel 同时生成 `//common:kernel_aarch64` 和
   `//common:kernel_aarch64_modules_prepare`，然后解压准备目录。
   Legacy 使用 `BUILD_CONFIG=common/build.config.gki.aarch64 build/build.sh` 初始化，
   再使用作者指定的宿主工具链及 sysroot 执行 `modules_prepare`。
4. 保留生成的内核配置。外部模块仍按原脚本暂时移走 `Module.symvers`、保留
   `modversions`、修复空扩展版本段，并恢复 `Module.symvers`。
5. 每个分支严格使用作者指定的 Clang 路径，缺失时失败，不换一个编译器继续构建。
6. 6.6、6.12、6.18 仍保留符号；其余版本只在用户选择时执行 `--strip-debug`。
7. 对生成的 ELF 检查架构、模块结构大小、入口 KCFI 类型标识、CFI 跳板、
   vermagic 功能标志、空版本段和编译器，发现已知 ABI 差异就阻止打包。

完整构建会花更多时间和磁盘空间，单版本超时为 180 分钟。
`MANIFEST_REVISION` 可覆盖默认 manifest 分支；要重现一次构建，使用保存的
`manifest.xml` 中的全部项目提交，而不能只固定内核源码一个提交。

## 安装脚本如何生成

`packer.sh` 将各版本 `.ko` 编码为 Base64，放进 `payload_*` shell 函数，
再添加按内核分支选择模块、解码到 `/data/local/tmp` 和执行 `insmod` 的逻辑。
它是模块加载脚本，不会修改 boot 分区，也不是完整内核刷机包。

Actions 收集本次运行的模块后调用同一个 `packer.sh`，逐字节核对脚本中解码出的
每个模块与 `release/modules/*.ko` 一致，再生成 SHA256SUMS。
单版本运行只含该版本，`all` 才包含七个版本。
不要把源码树中以前提交的 `install_driver.sh` 当成这次云编译产物；
应使用对应 Actions 的 `lsdriver-package/install_driver.sh` 或相应 Release 附件。

## 二进制报告与验证边界

基准摘要位于 `.github/reference/upstream-modules.json`，记录作者仓库提交、
安装脚本 SHA-256、每个模块 SHA-256、编译器及 ELF 信息。
每次构建的 `module-audit.json` 明确记录 `byte_identical`、实际/基准 vermagic、
新增/移除的导入符号，以及 `device_loading_tested: false`。
`module-commands.tar.gz`、`lsdriver.mod.c`、`module-compiler.txt` 和
`kernel.config` 用于进一步检查真实编译参数。

在安装了 `pyelftools` 的环境中，可离线重新检查一个模块：

```bash
python3 .github/scripts/module-audit.py 6.12-Android16.ko \
  --kernel 6.12-Android16 \
  --reference .github/reference/upstream-modules.json \
  --output module-audit.json
```

作者没有提供本地内核的固定 manifest、完整配置或工作区修改。
因此只能对齐公开构建流程并核对已知二进制特征，不能保证文件逐字节相同，
也不能保证所有厂商内核都可加载。当前仓库另有驱动生命周期和触摸协议改动，
本次修复不回退这些改动；导入符号变化（如 `kthread_stop`）会如实记录。
最终设备验证仍需要对应手机的 `uname -r`、`insmod` 错误及相关内核日志。
