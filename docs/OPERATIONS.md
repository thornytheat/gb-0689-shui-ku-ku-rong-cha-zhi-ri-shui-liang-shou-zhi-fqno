# 运行维护

主服务端口与Compose保持一致。数据保存在records命名卷，应用代码不需要写入权限。

```sh
docker compose logs --tail=80 app
docker compose restart app
docker compose exec -T app pytest -q
docker compose down --remove-orphans
```

健康检查GET /health。失败输入返回原因码，结构性错误422，过大文件413，陈旧复核409。
文件内容最大4MB。批次先计算再原子保存，失败不留半条记录。证据CSV按原始行号定位。

## 补传修订接口（M6）

- `POST /api/revision/<run_id>`：提交补传 CSV，派生替代日账并返回带检查点的任务；同内容重办幂等续跑。
- `GET /api/revision/jobs`、`GET /api/revision/job/<job_id>`：任务列表与详情（含受影响日、按日变化、待裁决冲突、留痕）。
- `POST /api/revision/job/<job_id>/adjudicate`：对待裁决同版冲突选定保留行（参数 station/time/ref）。
- `POST /api/revision/job/<job_id>/review`：替代批次复核，整数版本乐观锁，陈旧返回409。
- `POST /api/revision/job/<job_id>/switch`：复核完整后切换现行日账；可带 expected_active 做乐观锁，不匹配返回409且保留旧版。
- `GET /api/active`：现行日账指针与批次内容。

SQLite 新增 `sources`（原始凭证）、`revision_jobs`（任务与检查点）、`revisions`（替代批次补传留痕）、`active_ledger`（现行日账单行指针）四张表，均由应用启动时自动建表，无需手工迁移。原 `runs` 表结构与既有批次保持兼容。
