# `a7r3-usb-senser` — Sony A7R3 连接处理与服务会话基础设施

`a7r3-usb-senser` 是面向 **Sony Alpha 7R III (`ILCE-7RM3`)** 的主机侧 USB 模式状态机、USB 3.1 SuperSpeed 包对齐认证传输层、libusb 驱动接管辅助，以及 SENSER 节流诊断终端与 `/tmp_bt` 内存暂存会话 SDK。

> [!CAUTION]
> **硬件与连接安全边界**
> 1. 本仓库仅包含独立编写的 Python 源码与离线测试，不包含任何 Sony 专有固件二进制文件（`.so` / `.elf` / `.ko` / `.bin`），亦不内嵌第三方 `Sony-PMCA-RE` 源码（通过环境变量 `SONY_PMCA_ROOT` 在运行时按需外部引用）。
> 2. 连接实机前请阅读 [`docs/usb-hazards.md`](docs/usb-hazards.md)：切勿在低电量 `054c:0994`（USB Charger）模式下强行重试；切勿将高频临时文件写入机身持久化 VFAT 分区 `/tmp`（须使用内存文件系统 `/tmp_bt`）；切勿操作 `/sys/class/udc/dwc3.0/soft_connect`。

---

## 免责声明

> 大家请以学习为目的使用本仓库⚠️⚠️⚠️⚠️，[安全研究法律边界与合规须知](./DISCLAIMER.md#法规与合规参考)
>
> 本仓库的所有内容仅供学习和参考之用，禁止用于商业用途。任何人或组织不得将本仓库的内容用于非法用途或侵犯他人合法权益。本仓库所涉及的固件分析、系统逆向与硬件接口研究技术仅用于个人合法自有设备上的学习与研究，不得用于未经授权侵入或破坏他人设备、绕过技术保护措施、提取或传播第三方专有固件或其他非法行为。对于因使用本仓库内容而引起的任何法律责任、硬件损坏（变砖）、数据丢失或保修失效，本仓库及作者不承担任何责任。使用本仓库的内容即表示您同意本免责声明的所有条款和条件。
>
> 点击查看更为详细的免责声明。[点击跳转](./DISCLAIMER.md)

---

## 设计概览

整套固件研究与运行态扩展体系建立在无外部硬件调试口（不拆机焊接主板串口、不依赖外部底层刷机短接线）且不重刷固件分区的约束之上。`a7r3-usb-senser` 作为连接主机与相机的第一层基础设施，直接基于机身标准 USB 接口将设备从海量存储模式（`054c:0bff`）推进至厂商自定义服务会话（`054c:0336`，`bInterfaceClass = 0xFF`），使上层工具能够在用户态免驱直连机身并开展非破坏性探测与运行态加载。

针对物理链路与机内环境的边界特性，传输与会话层在协议细节上施加了严格约束：在 USB 3.1 SuperSpeed 链路上将原 `516` 字节认证握手读缓冲对齐至 `1024 / 576` 字节端点包长，消除 `wMaxPacketSize = 1024` 下的端点溢出；在诊断终端上实施 `16B / 2ms` 节流写入与有序起止边界标记，规避串口突发丢包与回显粘连；在文件传输上仅向真正的 `tmpfs` 挂载点 `/tmp_bt` 执行 `32 KiB` 分片哈希校验暂存，避免误写持久闪存 `/tmp`。

此外，会话层封装了原厂 `libsencore.so` 的 `ProductInfo`（`pfunc 0x10`）进程内插件调用接口，支持在主进程 `im.elf` 上下文中按需触发指定模块并在会话结束时自动执行 `dlclose` 释放句柄，从而在零修改原厂可执行文件的前提下提供可逆的运行态交互通道。

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
