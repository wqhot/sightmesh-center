# Center Reliable TrackEvent Inbox（阶段一：持久 HTTP 接入）

> **代码已提交，尚未做任何编译、单元测试或端到端验收。本模块当前不是 Center V1 全局关联实现。** 仅对 Edge 已存在的 JSON Batch/Blob 协议实现可选、持久且幂等的接入层。未来 NNG REQ/REP 的服务入口应复用本 Inbox 事务，不能复制另一份可靠性逻辑。

## 独立运行与地图服务共存

地图服务继续使用既有 8080 端口和 C++ / Python Map 服务：

```bash
python3 -m sightmesh_center --config config/center.json serve
```

Track Inbox 是**独立进程**，默认仅监听 loopback 的 18081 端口：

```bash
python3 -m sightmesh_center.track_ingest_server \
  --bind 127.0.0.1 --port 18081 \
  --db data/track-inbox.sqlite3
```

安装项目后也可使用 `sightmesh-track-inbox`。或在已配置 `config/center.json` 的环境下执行 `python3 -m sightmesh_center ingest`；`runtime.ingest` 包括 bind/port/db/token_file。

跨机联调时必需**只读于用户的 Token 文件**，示例：

```bash
mkdir -p ~/.config/sightmesh
umask 077
python3 -c 'import secrets; print(secrets.token_hex(32))' > ~/.config/sightmesh/track-inbox.token
chmod 600 ~/.config/sightmesh/track-inbox.token
python3 -m sightmesh_center.track_ingest_server \
  --bind 0.0.0.0 --port 18081 \
  --token-file ~/.config/sightmesh/track-inbox.token \
  --db data/track-inbox.sqlite3
```

禁止把密钥写进仓库、脚本或启动参数正文。HTTP Bearer Token 只提供身份校验，**不提供加密**；跨不可信网络必须使用 VPN / TLS 反向代理 / 隔离有线或专用传输域。服务拒绝没有 Token 的公网/局域网 bind。

### 现有 Edge 协议路径（无需迁移 protobuf）

| 方法 | 路径 | 输入 | 服务端响应 |
| --- | --- | --- | --- |
| `POST` | `/api/v1/mtmct/events/batch` | `JsonProtocolCodec::EncodeBatch` 原 JSON `{"version":1,"events":[...]}` | JSON `SyncAck`，可直接由 Edge 解码 |
| `PUT` | `/api/v1/mtmct/blobs/{sha256}` | 原始二进制，携带 `X-MTMCT-Blob-SHA256/Size` | 在 SQLite 中完成内容 SHA-256 校验后返回成功 |
| `GET` | `/health` | 无 | 只返回服务是否运行，不暴露数据 |

直接启用 Edge 时，与地图 HTTP 地址**不是同一个端口**。如果启用 bearer token，两端使用相同 Token 文件内容。Edge 侧配置详见 [Edge PR](https://github.com/wqhot/sightmesh-edge/pull/8)，只应在对应功能 PR 合并且有明确测试结论后使用。

## 持久性语义

使用 Python 内置 SQLite3；`WAL + synchronous=FULL + foreign_keys=ON`，单事务提交 `TrackEvent` 原始语义字段、完整 JSON、SHA-256 摘要和 Blob 引用；原始二进制 Blob 在内容哈希核对后以 BLOB 持久提交。

- 同一节点的 `seq` 来自**跨重启持久 Journal**，而非每次开机改变的 `session_id`。故 ACK / 主键以 `node_id + seq` 为基准，`session_id` 仍保存为事件属性。同一物理节点上不得开启两个**共用 node_id、却使用不同 spool** 的 Edge 进程。
- 同 `node_id + seq` 重放且内容相同，幂等成功；不同内容、不同 event_id 或同 event_id 不同序号均拒绝**整个批次**，不会推进 ACK。
- ACK `highest_contiguous_seq` 是**从 1 连续存在、且所有关联 Blob 已持久化**的最高序号。接收顺序乱序时保留缺口，不因为有高序号而提前确认。
- ACK 返回 `missing` 区间和 `missing_blobs` SHA 列表。存在缺失 Blob 时仍返回 `accepted:true`，保留未完成事件供补传；只有成功补齐 Blob 并提交后才向前推进连续 ACK。
- 任何数据库错误都不能作为成功 ACK；服务不能连接/磁盘不可写时 Edge 在本机 Journal 重试，避免吞掉关键帧和 TrackEvent。
- 不实现强制无限度去重缓存 GC：本阶段事件+Blob 永久保留，后续必须按业务 snapshot / retention / source checkpoint 加入受控清理机制，不能从 DB 直接删尚未完成的可靠消息。

### 已有 Edge spool 的部署前置

对**已有数千事件且本地已 ACK/清理早期事件**的 Edge，不能把新的、完全空白的 Center DB 当作历史已经提交：这将造成 `seq` 从高值开始、Center 连续 ACK 卡在 0 的安全保护状态。先人工迁移验证后的 Center 历史/可信 ACK checkpoint，或使用**全新的 node_id + spool** 发起新流；本 PR 故意不提供未经验证的自动跳过历史接口。

## 此 PR 不承诺

- **不是 NNG 的持久接收通道**：NNG 可靠 REQ/REP adapter、Protobuf TrackEvent/Blob 内容无损映射仍待下一阶段实现。现有 Wire PR 的 NNG PUB/SUB 只能用于可丢弃实时状态。
- 不是全局 ReID/关联/GlobalTrack/WorldState：Center V1 仍须实现事件时间重建、Tracklet Repository、候选生成、求解器和全局状态修订。
- 不是外部开放服务安全基线：仍缺少设备间双向证书、审计、日志轮转、限流和数据库规模约束。先仅用于隔离联调网。

## 留待最终集中验收（此阶段不执行）

```bash
python3 -m unittest tests.test_track_inbox -v
```

并在 x86 / D2000 实际运行环境上验证：断电前后 SQLite/WAL ACK 不回退；丢 Blob → ACK 卡住→补 Blob→正常前移；乱序/重复/冲突批次、错误 SHA、安全配置；Edge 发真实 Journal/Blob 到 Center 并断链恢复；多节点同号 local ID 不冲突；性能/存储上限。
