# Sony ILCE-7RM3 (A7R3) USB 与会话操作风险手册 (`usb-hazards.md`)

本文档总结了在 macOS 主机与 Sony Alpha 7R III (`ILCE-7RM3`, FW 3.10) 进行 USB 模式切换、SENSER 服务会话通信及临时文件暂存过程中经实机验证的四大高危陷阱与强制安全边界。任何自动化脚本或二开工具必须严格遵循以下 **Fail-Closed（失效即阻断）** 规则。

---

## 1. `054c:0994` USB Charger（充电模式）死锁陷阱

### 现象与描述符特征
当相机电池电量跌落至机身阈值以下，或在插着 USB 线时进入深度休眠/挂起状态，相机的电源管理芯片与 USB 控制器会脱离常规相机协议栈，重枚举为 **`054c:0994` ("USB Charger")**：
- `idVendor = 0x054c`, `idProduct = 0x0994`
- `iSerialNumber = 0`（无序列号字符串）
- `bConfigurationValue = 1`, `bMaxPower = 250` (`500 mA`)
- 仅暴露单一 **HID 接口** (`bInterfaceClass = 3`, `bInterfaceSubClass = 0`, `bInterfaceProtocol = 0`) 及 1 个 8 字节中断输入端点 (`0x81`, `bmAttributes = 3`, `wMaxPacketSize = 8`)。
- **完全不存在** Mass Storage (`0x08`)、PTP (`0x06`) 或 SENSER (`0xFF`) 接口。

### 风险与强制处置准则
1. **禁止盲目重试或发送 Vendor 控制传输**：`054c:0994` 状态下机身应用处理器未挂载任何命令通道，向其发送 USB Reset 或非标准控制请求无法唤醒系统，反而可能干扰充电管理。
2. **立即作废旧会话句柄**：一旦 `a7r3_senser.transport.classify_usb_mode()` 识别到 `USBMode.CHARGER` (`is_hazard_trap=True`)，调用方必须立即终止轮询，将之前打开的所有设备句柄与会话 Nonce 标记为失效（Quarantined）。
3. **物理恢复流程**：拔下 USB 线 -> 更换满电电池（或关闭电源开关重新开机唤醒屏幕）-> 确认机身 USB 连接模式为 **Mass Storage（海量存储器）** -> 重新插入 USB 线并从零开始执行身份核验。

---

## 2. 持久化 `/tmp` (`nflasha2` VFAT) 与易失性 `/tmp_bt` (`tmpfs`) 混淆风险

### 机身挂载表真相
在常规桌面 Linux 系统中，`/tmp` 通常是内存文件系统 (`tmpfs`)；但在 **Sony A7R3** 的嵌入式 Linux 环境中，实时挂载表 (`/proc/mounts`) 结构截然不同：

| 挂载路径 | 底层块设备 / 类型 | 文件系统 | 容量量级 | 断电/重启后行为 | 安全属性 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **`/tmp`** | `/dev/nflasha2` | **VFAT (NAND Flash)** | `20 MiB` (`offset 8 MiB..28 MiB`) | **持久保留！**（且 `/etc/profile` 软链接指向 `/tmp/profile`） | **严禁用于高频中转或临时大文件！** |
| **`/tmp_bt`** | `none` (RAM) | **`tmpfs`** | `~1 MiB` | **断电即清空** | **唯一合法的易失性内存暂存区** |

### 引发的两类严重后果
1. **闪存磨损与残留污染**：若将调试脚本、动态库分片或采集中间文件写入 `/tmp`，不仅会直接消耗机身内置 NAND (`nflasha2`) 擦写寿命，还会在重启后永久残留。
2. **全盘镜像采集自篡改（Self-Mutation）**：在导出 `/dev/nflasha` 逻辑镜像时，如果把 `dd` 窗口暂存到 `/tmp`，每一次分片写入都会实时改写正在被读取的 `/dev/nflasha` 第 `16..55` 号 `512 KiB` 分片（即 `nflasha2` 所在区间），导致全盘哈希永远无法闭合。

### 强制安全规范
- 所有临时文件上传与 NAND 读取窗口**必须且只能**使用 `/tmp_bt` 下的独立随机子目录（如 `/tmp_bt/a7r3-upload-<uuid>`）。
- 操作前必须执行 `busybox grep '^none /tmp_bt tmpfs ' /proc/mounts` 与 `test -d /tmp_bt && test ! -L /tmp_bt` 双重校验。
- 鉴于 `/tmp_bt` 总容量仅约 `1 MiB`，`a7r3_senser.session.prepare_ram_bundle()` 严格限制单文件 `<= 192 KiB`、整包 `<= 256 KiB`，组装校验完成后立即 `rm` 清理分片文件。

---

## 3. 严禁使用 `/sys/class/udc/dwc3.0/soft_connect` (`usb_soft_cycle`) 软重连

### 实机失效记录 (`FAILED_TO_REENUMERATE`)
在尝试不插拔物理线缆让相机从 SENSER 服务模式切换回普通拍摄/USB 模式时，曾测试向内核 DesignWare USB3 控制器节点 `/sys/class/udc/dwc3.0/soft_connect` 先写入 `"disconnect\n"`，延时 4 秒后再写入 `"connect\n"`（配合后台管道看门狗进程）。

实机结果证明该方案**彻底失效且不可自动恢复**：
1. 写入 `"disconnect\n"` 后，主机侧 `054c:0336` 立即消失，但后续写入 `"connect\n"`（即使 `write()` 系统调用返回 `0`）**永远不会触发 macOS 主机侧重新枚举任何 Sony USB 设备**。
2. 内核源码级原因：在 Sony 定制内核 (`CONFIG_USB_FUSB_OTG` + `dwc3` gadget) 中，`soft_connect` 断开路径会关闭 `EP0/EP1` 并调用 gadget driver `disconnect` 回调清理状态机，而对称的软件 `connect` 路径缺少 OTG VBUS/PHY 状态重协商，导致控制器停留在悬空态，只能通过**物理拔插 USB 线**恢复。

### 正确且安全的模式切换方式
- **永久固化 Mass Storage 默认模式**：在 SENSER 会话中确保相机配置项 `0x0107001f` 在持久化 Backup 中为 `0x02` (`MSC`) 而非 `0x04` (`PTP`)，随后调用正常的 `SonySenserAuthDevice.stop()` 退出服务会话，或通过机身受控重启自然重枚举回 `054c:0bff`。
- **绝不向 `/sys/class/udc/dwc3.0/soft_connect` 写入任何字节**。

---

## 4. USB 3.1 SuperSpeed `516` 字节认证读取溢出 (`USBError EIO`)

### 根因剖析
A7R3 采用 Type-C USB 3.1 Gen 1 接口。在 SuperSpeed Mass Storage (`054c:0bff`) 模式下，Bulk IN 端点 `0x81` 的 `wMaxPacketSize = 1024`（而在 USB 2.0 High-Speed 下为 `512`，Full-Speed 下为 `64`）。

上游 `Sony-PMCA-RE` 的 `SonySenserAuthDevice` 在三次握手认证阶段直接调用 `ep.read(516)`。在 macOS `libusb` (`IOUSBHost` Darwin 后端) + SuperSpeed 链路下，提交非 `wMaxPacketSize` 整数倍的 `516` 字节读取请求会在第 3 次认证交互时触发主机控制器拒绝/溢出，抛出 `USBError(5, 'Input/Output Error')`！

### 协议无损修复 (`auth_read_size` / `read_auth_response`)
`a7r3_senser.transport` 在主机侧将传给 `libusb` 的缓冲区长度向上取整至 `wMaxPacketSize` 的最小整数倍：
- `wMaxPacketSize = 1024` (USB 3.1 SuperSpeed) -> 提交 `1024` 字节缓冲区
- `wMaxPacketSize = 512` (USB 2.0 High-Speed) -> 提交 `1024` 字节缓冲区 (`2 × 512`)
- `wMaxPacketSize = 64` (USB Full-Speed) -> 提交 `576` 字节缓冲区 (`9 × 64`)

相机端在发送完精确的 `516` 字节负载后会自然结束短包（Short Packet）传输；主机 `read_auth_response()` 收到返回后**严格校验 `len(data) == 516`**（绝不截断或补零，绝不在 `EIO` 时盲目重放握手包），从而在 100% 保持原厂协议帧不变的前提下彻底消除 USB3 `EIO` 故障。
