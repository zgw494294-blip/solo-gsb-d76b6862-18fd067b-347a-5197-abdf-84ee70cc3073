# Recovery Planning HTTP API

用 **Python 3.12 + FastAPI + SQLite** 实现的数据库恢复规划服务。

- 批量**原子登记**数据库快照与日志片清单（单批总数 ≤ 100）。
- 给定 UTC 查询时刻，只使用该时刻**已生成**（`created_at <= at`）的清单。
- 从快照的 `applied_seq + 1` 起，**仅使用同格式**日志片连续回放；每片从下一未回放序号接至片尾，重叠部分不重放，遇缺口即停。
- 按规则选出最优恢复点：**终点序号最大** → 快照 `applied_seq` 最大 → 快照 `id` 字典序最小 → **回放片数最少** → **回放 id 序列字典序最小**。

## 一键启动（Docker Compose）

```bash
docker compose up --build
```

- 服务监听宿主机端口 **8080**（容器内 8000）。
- SQLite 数据文件 `/data/recovery.db` 在镜像启动时自动初始化建表，并持久化到命名卷 `recovery-data`。
- 健康检查：`GET http://localhost:8080/healthz`

停止：`docker compose down`（保留数据）；连数据一起清除：`docker compose down -v`。

也可以不用容器：

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8080
# SQLite 路径可用环境变量 RECOVERY_DB 覆盖（默认 /data/recovery.db）
```

## 数据模型

| 类型 | 字段 | 约束 |
| --- | --- | --- |
| 快照 snapshot | `id` | 非空字符串；快照与日志片**共享唯一 id 命名空间** |
|  | `created_at` | 感知时区的 RFC 3339/ISO-8601 UTC 时刻（如 `...Z`、`+08:00`），存储时归一化为 UTC |
|  | `applied_seq` | 全局非负整数（`>= 0`） |
|  | `format` | 非空格式代号，日志片必须与之相同才可回放 |
| 日志片 segment | `id` | 同上 |
|  | `created_at` | 同上 |
|  | `format` | 格式代号 |
|  | `first_seq` / `last_seq` | 正整数（`>= 1`）且 `first_seq <= last_seq`，闭区间 |

额外字段、类型错误（含布尔值冒充整数）、空白 id 等一律判为非法。

**整批原子性**：一个批次内任何字段非法、批次总数 > 100、批次内 id 重复，或 id 与库中已有记录冲突，整批拒绝（422/409），不写入任何记录。

## 回放与择优语义

对某快照（`applied_seq = A`，格式 `F`），回放游标从 `A + 1` 开始：

1. 只考虑 `created_at <= at` 且 `format = F` 的日志片。
2. 游标为 `c` 时，可接的片满足 `first_seq <= c <= last_seq`；该片**实际回放区间为 `[c, last_seq]`**（`[first_seq, c-1]` 已回放，跳过，绝不重放），游标变为 `last_seq + 1`。
3. 无片覆盖 `c` 即为缺口，停止，恢复终点为 `c - 1`。
4. 在所有可行链中按上述优先级选出全局最优快照与链。

## HTTP 示例（端口 8080）

### 1) 健康检查

```bash
curl http://localhost:8080/healthz
# {"status":"ok"}
```

### 2) 批量登记（原子）

```bash
curl -X POST http://localhost:8080/v1/batches \
  -H 'Content-Type: application/json' \
  -d '{
    "snapshots": [
      {"id":"snap-100","created_at":"2026-09-20T00:00:00Z","applied_seq":100,"format":"binlog-v1"}
    ],
    "segments": [
      {"id":"log-3","created_at":"2026-09-23T00:00:00Z","format":"binlog-v1","first_seq":101,"last_seq":130},
      {"id":"log-4","created_at":"2026-10-01T00:00:00Z","format":"binlog-v1","first_seq":131,"last_seq":200}
    ]
  }'
# {"status":"registered","count":{"snapshots":1,"segments":2,"total":3}}
```

非法批次示例（`first_seq > last_seq` → 422，整批不入库）：

```bash
curl -i -X POST http://localhost:8080/v1/batches \
  -H 'Content-Type: application/json' \
  -d '{"segments":[{"id":"bad","created_at":"2026-09-21T00:00:00Z","format":"binlog-v1","first_seq":9,"last_seq":3}]}'
```

重复 id（批内或与库中冲突 → 409，整批不入库）：

```bash
curl -i -X POST http://localhost:8080/v1/batches \
  -H 'Content-Type: application/json' \
  -d '{"snapshots":[{"id":"snap-100","created_at":"2026-09-20T00:00:00Z","applied_seq":101,"format":"binlog-v1"}]}'
```

### 3) 查询恢复规划

`at` 为必填的感知时区时刻；服务只使用生成时间不晚于它的清单。

```bash
curl 'http://localhost:8080/v1/recovery-plan?at=2026-10-02T00:00:00Z'
```

```json
{
  "status": "recovery_point",
  "at": "2026-10-02T00:00:00Z",
  "snapshot": {
    "id": "snap-100",
    "created_at": "2026-09-20T00:00:00Z",
    "applied_seq": 100,
    "format": "binlog-v1"
  },
  "replays": [
    {
      "segment_id": "log-3",
      "segment_range": {"first_seq": 101, "last_seq": 130},
      "replayed_range": {"from_seq": 101, "to_seq": 130}
    },
    {
      "segment_id": "log-4",
      "segment_range": {"first_seq": 131, "last_seq": 200},
      "replayed_range": {"from_seq": 131, "to_seq": 200}
    }
  ],
  "segment_count": 2,
  "end_seq": 200
}
```

重叠示例：若游标为 11、片区间为 `[1,12]`，`replayed_range` 为 `{"from_seq":11,"to_seq":12}`；之后序号 13 无片覆盖时停止，`end_seq` 为 12。

在任何快照生成之前查询，明确返回无恢复点（HTTP 200）：

```bash
curl 'http://localhost:8080/v1/recovery-plan?at=2026-01-01T00:00:00Z'
# {"status":"no_recovery_point","at":"2026-01-01T00:00:00Z",
#  "message":"no snapshot is visible at the given instant"}
```

时区偏移会被归一化到 UTC，例如 `at=2026-10-02T08:00:00+08:00` 等价于 `at=2026-10-02T00:00:00Z`。

### 4) 查看清单（调试用）

```bash
curl http://localhost:8080/v1/inventory                       # 全部
curl 'http://localhost:8080/v1/inventory?at=2026-09-30T00:00:00Z'  # 仅该时刻已生成
```

交互式 API 文档（Swagger UI）：<http://localhost:8080/docs>。

## 状态码

| 码 | 含义 |
| --- | --- |
| 201 | 批次已登记 |
| 200 | 规划/查询成功（含无可用快照的 `no_recovery_point`） |
| 400 | 请求体不是合法 JSON |
| 409 | id 重复（批内或与库中冲突），整批未入库 |
| 422 | 字段非法 / 批次为空或超过 100 / `at` 缺失或不是感知时区时刻，整批未入库 |

## 测试

```bash
pip install -r requirements.txt httpx
python3 test_planner.py                                   # 规划算法（仅标准库）
RECOVERY_DB=/tmp/test.db python3 -m unittest test_api -v  # HTTP 端到端
```

## 目录结构

```
app/
  __init__.py
  models.py    # Pydantic 严格校验（非法批次在入库前被拒绝）
  db.py        # SQLite 连接、幂等建表
  planner.py   # 恢复规划核心算法（分层枚举 + 同游标去重 + 字典序择优）
  main.py      # FastAPI 路由
Dockerfile
docker-compose.yml
requirements.txt
test_planner.py
test_api.py
```
