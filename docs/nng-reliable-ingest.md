# Center 可选 NNG 可靠接收网关（阶段二）

> **当前为未编译/未联调的可选代码分支**。不替换地图服务，不生成第二份 SQLite Inbox；NNG REQ/REP 只提供传输应答，业务 ACK 由 [Center Durable Inbox](reliable-track-inbox.md) 决定。

## 与原有服务的部署关系

```text
Edge ReliableEdgeJournal / ReliableSyncService
                   │
          ┌────────┴────────┐
          ▼                 ▼
NNG REQ/REP RPC          HTTP Push（回退）
          │                 │
Center NNG Bridge           │
          │                 │
          └── HTTP 127.0.0.1:18081
                        │
             Center DurableInbox (SQLite)
                        │
              contiguous_seq + Blob
              durable SyncAck
```

关键点：同一节点的 NNG 和 HTTP **必须落到同一个 Center SQLite 文件**；边缘 NNG 请求发送后若 ACK 丢失，可以安全地通过 HTTP 原样重试；重复批次的 SHA/seq 冲突由 Inbox 统一判断。

## 构建（独立于地图 importer）

依赖源：SightMesh Wire 可靠 RPC 分支（在本功能完成集中验证后应锁定到固定 SHA）。通过 ezvcpkg/triplet 提供 `protobuf`、`nng`、`libcurl`，桥接程序没有 Assimp/Open3D/OctoMap/OR-Tools 依赖：

```bash
cmake -S cpp/transport -B build/transport \
  -DSIGHTMESH_WIRE_ROOT=/path/to/sightmesh/wire \
  -DCMAKE_TOOLCHAIN_FILE=/path/to/ezvcpkg/scripts/buildsystems/vcpkg.cmake
cmake --build build/transport --target sightmesh-nng-inbox-bridge
```

**本轮不执行以上命令；构建/链接和 D2000 sysroot 兼容性留到统一验收。**

## 启动

先启动同一数据库唯一的持久 Inbox：

```bash
python3 -m sightmesh_center.track_ingest_server \
  --bind 127.0.0.1 --port 18081 \
  --db data/track-inbox.sqlite3 \
  --token-file ~/.config/sightmesh/track-inbox.token
```

再启动 NNG 网关（默认只监听 loopback）：

```bash
build/transport/sightmesh-nng-inbox-bridge \
  --listen tcp://127.0.0.1:19703 \
  --inbox-port 18081 \
  --token-file ~/.config/sightmesh/track-inbox.token
```

跨机时在可信 VPN/隔离网络使用 `--listen tcp://0.0.0.0:19703`。非 loopback 必须提供 owner-only Token 文件；**NNG TCP 明文携带 Token，认证不是加密，生产环境必须外包 TLS/WireGuard/专网安全通道**。不能直接向公网开放此服务。NNG 和 HTTP 两端读取相同 Token 内容，不将 Token 输出至日志。

## RPC 封装与可靠性边界

消息使用 `sightmesh.wire.v1.ReliableRpcRequest / ReliableRpcReply`：`schema_major`、`request_id`、`service`、`body`、`bearer_token`。只允许两个既有业务路由：

- `/api/v1/mtmct/events/batch`：内部原 `JsonProtocolCodec` 生成的 JSON Batch，未经任何质量/协方差的字段裁剪；
- `/api/v1/mtmct/blobs/{sha256}`：字节原样转发，Center 在单一 DurableInbox 用 SHA256 对内容验证。

这个 Protobuf **只承担 RPC 外壳**，不是将 TrackEvent 全量结构转换为新 Proto 类型。后者必须先完成逐字段 lossless schema、时间/坐标/质量/Keyframe/Blob 语义核对，避免默默丢掉 `localization_quality`。

服务端只允许 NNG 单条消息最大 20 MiB，批次最大 4 MiB、Blob 最大 16 MiB；只向本机固定地址 `127.0.0.1:18081` 转发，不接收任意 URL，从而避免 NNG 客户端把它变成通用 HTTP 代理。转发超时、磁盘错误都给非 2xx 状态，**永不伪造成功 ACK**。

## 最后集中验收

1. 原 Edge 同一批次经 HTTP 与 NNG 转发进入**同一 SQLite 文件**，重复提交不产生第二条事件；网络 ACK 丢失时可走 HTTP 回退。
2. 顺序缺口、已上传/未上传 Blob、SHA 冲突、持久事务成功但应答丢失、Center 重启与 Edge session 改变。
3. 模拟 401、409、500、503，确认 4xx/冲突不会触发另一条通道隐式绕过，5xx/传输错误才回退。
4. 各部署目标编译，特别是 D2000 ARM64 工具链、Protobuf ABI、NNG/CURL vcpkg 包及内存上限。
5. 不开启 NNG 选项时 Python/C++ 地图服务、Render SSE/NNG 状态与现有回放完全不受影响。

**这部分不涉及 Global Association、Tracklet Graph/OR-Tools 或 Center→Render GlobalTrack 的业务实现。**
