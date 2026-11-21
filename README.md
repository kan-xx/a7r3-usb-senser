# `a7r3-usb-senser` — Sony A7R3 连接处理与服务会话基础设施

`a7r3-usb-senser` 是面向 **Sony Alpha 7R III (`ILCE-7RM3`)** 的主机到相机 USB 模式分类、**USB 3.1 SuperSpeed `516B -> 1024B/576B` 包对齐认证传输层**、libusb 驱动状态管理以及 SENSER 节流诊断终端与 `/tmp_bt` 内存暂存会话 SDK。

> [!CAUTION]
> **硬件与数据安全红线**
> 1. 本仓库**不包含任何 Sony 专有固件二进制文件（`.so`/`.elf`/`.ko`/`.bin`）**，亦不内嵌第三方 `Sony-PMCA-RE` 源码（通过环境变量 `SONY_PMCA_ROOT` 按需外部引用）。
> 2. 操作实机前请务必完整阅读 [`docs/usb-hazards.md`](docs/usb-hazards.md)：严禁在低电量 `054c:0994`（USB Charger）陷阱态下强行重试；严禁将高频临时文件写入持久化 VFAT 分区 `/tmp`（必须使用真 `tmpfs` `/tmp_bt`）；严禁操作 `/sys/class/udc/dwc3.0/soft_connect`！

---

## 免责声明

> 大家请以学习为目的使用本仓库⚠️⚠️⚠️⚠️，[安全研究法律边界与合规须知](./DISCLAIMER.md#法规与合规参考)
>
> 本仓库的所有内容仅供学习和参考之用，禁止用于商业用途。任何人或组织不得将本仓库的内容用于非法用途或侵犯他人合法权益。本仓库所涉及的固件分析、系统逆向与硬件接口研究技术仅用于个人合法自有设备上的学习与研究，不得用于未经授权侵入或破坏他人设备、绕过技术保护措施、提取或传播第三方专有固件或其他非法行为。对于因使用本仓库内容而引起的任何法律责任、硬件损坏（变砖）、数据丢失或保修失效，本仓库及作者不承担任何责任。使用本仓库的内容即表示您同意本免责声明的所有条款和条件。
>
> 点击查看更为详细的免责声明。[点击跳转](./DISCLAIMER.md)

---

## 技术路线与设计考量（与传统刷机改包方案的本质差异）

目前开源社区针对索尼相机固件的研究（如早期 `fwtool` 重打包、拆机焊接 UART/编程器，或近年针对新机型的自定义 `BODYDATA.DAT` 刷机包），主流做法是**直接静态修改内核镜像或原厂核心库（如 `appFw.so`）指令字节、替换签名公钥，再将修改后的固件烧录进内部闪存**。

本体系探索并验证了一条截然不同的工程路线：**零原厂代码段篡改（Zero-`.text`-Mutation）的外挂式模块化常驻 + 运行时内存/内核动态补丁（Runtime In-Memory Patching）+ 机内双槽自愈事务**。
> [!TIP]
> **本仓库在该路线中的角色**：提供**免拆机、免刷 Bootloader 的安全运行态入口**——无需焊接主板 UART 或使用硬件短接器进 Fastboot，直接通过 USB 服务协议、SuperSpeed `516B -> 1024B` 包对齐握手与 `/tmp_bt` 纯内存暂存建立受控会话，从源头杜绝误写持久化闪存。

| 维度 | 传统/主流固件破解方案（静态改包 + 烧录硬件闪存） | 本体系路线（外挂动态加载 + 运行态内存/内核补丁） |
| :--- | :--- | :--- |
| **内核与原厂库修改方式** | 直接改写内核镜像（`uImage`）或原厂 `.so` 指令字节，替换 RSA 密钥后走升级脚本刷写 `nflash` 分区 | **内核分区镜像、RTOS 固件与原厂核心库的磁盘可执行代码段 100% 零字节改动** |
| **补丁生效机制** | **静态落盘固化**：靠刷写修改版固件覆盖原厂程序，以修改机型宏解锁同代已有隐藏分支为主 | **运行态动态干预**：仅对 `libIMDB.so` 追加纯数据描述符加载自研模块；在 RAM 中完成 GOT 替换、UI 回调桥接与动态 `insmod` 内核 `.ko` 探针（断电即净） |
| **防变砖与救砖手段** | 高度依赖拆机焊接主板微型 UART 串口，或使用外部硬件短接线强制拉入底层 `Fastboot` 救砖 | **纯软件自闭环防砖**：内置早期 `a7r-boot-guard` 双槽日志守护（60s 超时自动回滚原厂）+ `nlink >= 1` 映射保留原子替换，零硬件救砖口下零变砖 |
| **验证方式与开源洁净度** | 依赖静态反编译猜测 + 刷机试错；分发修改版固件包易触碰厂商专有代码版权红线 | **跨三核 Unicorn 原码无桩执行先验排雷**；仓库 **100% 为净室原创源码**，不携带、不分发一字节厂商专有固件 |

### 为什么要这样设计？（四大核心考量）

1. **单台主力机“零硬件救砖口”下的安全底线**：在不拆机焊串口、无底层 Fastboot 刷机线的真实条件下，直接改内核重刷闪存一旦出错即永久变砖。因此我们坚持**不重刷内核与固件大包、先建双槽自动回滚守护再上线功能、未闭合所有权的硬件异常一律执行失效关闭隔离（Fail-Closed Quarantine）**。
2. **彻底的净室（Clean-Room）开源合规**：不修改、不重打包原厂二进制，意味着开源仓库只需发布纯自研 C / ASM / Python 源码与构建脚本，从根源上规避专有固件分发风险。
3. **把试错成本消灭在接触硬件之前**：通过将原厂 ARM Linux、Thumb RTOS、MIPS32 协处理器机器码直接放入 Unicorn 无桩环境与 QEMU 原厂加载器靶场执行，在离线阶段即可精准捕获如 `ld-2.11.2.so` `DT_RELCOUNT` 覆写漏洞与传感器 8 位截断反例。
4. **突破“改宏开关”上限，从零构建原生子系统**：针对老架构原厂根本不存在的功能（无自定义音频导入、菜单触控事件位全零、回放无双指手势状态机），以分层插件底座赋予封闭固件真正的可扩展能力。

---

## 系统架构

```mermaid
flowchart TD
    HostCLI["主机 CLI / Python SDK (a7r3-senser)"] --> Classifier["USB 模式分类器 (054c VID)"]
    Classifier -->|"054c:0bff (MSC)"| DriverDetach["libusb 驱动状态检查与接口声明"]
    Classifier -->|"054c:0c33 (PTP)"| RejectPTP["拒绝服务进入 (提示切换至海量存储器)"]
    Classifier -->|"054c:0994 (Charger)"| TrapStop["充电陷阱熔断 (Fail-Closed 停止重试)"]
    Classifier -->|"054c:0336 (SENSER 0xFF)"| UnprivSession["非特权 SENSER 会话 (SenserSession)"]

    DriverDetach --> AuthTransport["USB3 包对齐认证传输 (516B -> 1024B / 576B)"]
    AuthTransport -->|"重枚举为 Vendor Class 0xFF"| UnprivSession

    UnprivSession --> Terminal["16B / 2ms 节流诊断终端 (回显安全边界标记)"]
    UnprivSession --> RamStage["/tmp_bt tmpfs 有界分片暂存 (32 KiB 块校验 + 独占日志)"]
    UnprivSession --> PluginRPC["ProductInfo (pfunc 0x10) 插件包编解码 (libproNN.so)"]
```

---

## 目录结构

```text
a7r3-usb-senser/
├── DISCLAIMER.md                    # 详细法律合规与硬件风险免责声明
├── LICENSE                          # MIT 许可证 + 商标与硬件安全声明
├── Makefile                         # 离线单元测试与清理入口
├── pyproject.toml                   # 标准 PEP 621 打包配置与 a7r3-senser CLI 入口
├── docs/
│   └── usb-hazards.md               # 054c:0994 陷阱、/tmp vs /tmp_bt、soft_connect 禁用详解
├── src/a7r3_senser/
│   ├── __init__.py                  # SDK 公共导出接口
│   ├── transport.py                 # USB 模式分类、516B->1024B/576B 对齐读取、MSC->SENSER 状态机
│   ├── terminal.py                  # 16B/2ms 节流写入、ordered_terminal_markers()、终端恢复
│   ├── session.py                   # SenserSession、/tmp_bt 有界暂存、ProductInfo 0x10 插件辅助
│   ├── driver.py                    # 主机 USB 驱动探测/解绑/恢复与只读元数据采集
│   └── cli.py                       # a7r3-senser 命令行工具
└── tests/                           # 100% 离线自包含单元测试 (无需连接相机或专有固件)
    ├── test_transport.py
    └── test_terminal_and_staging.py
```

---

## 快速开始与环境变量配置

### 1. 安装与运行离线测试

无需连接相机或安装外部固件文件，直接运行完整离线测试套件：

```bash
make test
```

如需启用实际 USB 通信功能，请在虚拟环境中安装可选 USB 依赖并指定外部 `Sony-PMCA-RE` 路径：

```bash
pip install -e ".[usb,dev]"
export SONY_PMCA_ROOT="/path/to/Sony-PMCA-RE"
export A7R3_USB_SERIAL="YOUR_CAMERA_USB_SERIAL"
```

### 2. CLI 常用命令示例

```bash
# 1. 枚举当前连接的 Sony USB 设备并自动识别模式 (MSC / PTP / SENSER / CHARGER)
a7r3-senser list

# 2. 只读校验 MSC 设备描述符与端点包长 (不解绑驱动、不发送切换指令)
a7r3-senser enter --serial "$A7R3_USB_SERIAL" --dry-run

# 3. 从 Mass Storage (054c:0bff) 切换至 SENSER 服务模式 (054c:0336)
sudo SONY_PMCA_ROOT="$SONY_PMCA_ROOT" a7r3-senser enter \
    --serial "$A7R3_USB_SERIAL" \
    --report /tmp/a7r3-entry-report.json

# 4. 异常中断后请求恢复主机海量存储内核驱动并轮询验证
sudo a7r3-senser restore --serial "$A7R3_USB_SERIAL"
a7r3-senser verify-restore --serial "$A7R3_USB_SERIAL"
```

### 3. Python 会话与 `/tmp_bt` 内存暂存示例

当相机已枚举为 `054c:0336`（USB Vendor-Specific `0xFF`）时：

```python
import hashlib
from pathlib import Path
from a7r3_senser import SenserSession

payload = b"\x7fELF..."  # 自行编译且通过 DT_RELCOUNT=0 审计的模块字节
digest = hashlib.sha256(payload).hexdigest()

with SenserSession(enable_terminal=True) as sess:
    # 16B / 2ms 节流安全执行诊断命令，自动过滤终端回显干扰
    mounts = sess.terminal.execute("cat /proc/mounts")
    assert b"none /tmp_bt tmpfs " in mounts

    # 将模块安全分片上传至易失性 /tmp_bt (绝不触碰持久化 /tmp)
    staged_paths = sess.stage_ram_bundle(
        {"libcustom_probe.so": (payload, digest)},
        label="probe-trial",
        journal_path=Path("./stage-journal.json"),
    )

    # 通过 ProductInfo (pfunc 0x10) 发送 0xFA00 插件指令
    status, reply = sess.send_plugin(0xFA00, b"PING_PAYLOAD")
```

---

## 安全警告与故障恢复准则

1. **绝不在未知连接状态下自动重放写操作**：若 USB 传输抛出 `USBError(5, 'Input/Output Error')` 或超时，主机侧无法仅凭异常区分“命令未送达”与“相机正在重启”。必须将当前会话标记为 `STOPPED_NO_RETRY`，重新枚举并验证 `boot_id` 与文件哈希后再决定下一步。
2. **保持电量充足**：进入服务会话前建议机身电量 `>= 50%`，防止调试中途跌入 `054c:0994` 充电模式。
3. **退出会话自动释放句柄**：请始终使用 `with SenserSession(...)` 上下文管理器，确保退出时执行 `auth.stop()`，触发机身 `libsencore.so` 自动 `dlclose` 会话期间加载的插件句柄并恢复正常 USB 模式。
