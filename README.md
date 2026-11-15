# 播出简报异常编排

异常摘要与定时分发的真实业务分析小型项目。完整模拟客户、痛点、上下游、算法公式与一天工作剧本见 [BUSINESS.md](BUSINESS.md)。

## 模块与业务流程
数据接入（JSON/CSV、字段验证、批次指纹）→ 参数化分析（异步 Python worker）→ 结果复核（指标、图形、明细、解释、批准）→ 定时分发（本地收件箱、CSV/JSON、SHA256）→ 备用站交接（整包导出/校验/接收）；另有业务总览和审计轨迹。
三个贯穿全栈的实体：Dataset 数据批次、Job 分析任务、Report 分发计划；辅助 User、Audit、Delivery、HandoverImport/HandoverMapping。

## 快速启动
```bash
cp .env.example .env
docker compose up -d --build --wait
docker compose exec -T backend python -m scripts.seed_demo
```
访问 http://localhost:18219；后端文档 http://localhost:19219/docs；数据库宿主端口 17719。
`SEED_DEMO=true` 自动幂等 seed；不覆盖用户修改。手动 seed 命令可重复执行。

## 演示账号与权限
|账号|密码|真实权限|
|---|---|---|
|admin@demo.local|Demo123!|接入、执行、批准、分发、暂停、读取与下载；导出/接收交接包|
|analyst@demo.local|Demo123!|接入、执行、读取与下载；不能批准或安排分发；可导出/接收交接包|
|viewer@demo.local|Demo123!|只读与下载（可下载交接包）；不可上传、接收或导入交接包|

## 备用站交接
主站「交接包」页可导出 ZIP：manifest 清单 + 每个已批准简报及其原始日记、分析参数、分发计划和已投递 CSV/JSON 原始字节，每个文件带大小与 SHA256。
备用站上传后先进入「待校验」，逐项核对 ZIP 可读、清单有效、文件齐全未改动、业务内容可复核、包内关联和归档字节；任一项缺件或被改动即整包拒绝，不能接收。
接收按原始日记 → 简报 → 分发计划 → 归档的顺序推进并记录进度。中断后再次发起接收会从检查点继续，整包重试不会新增简报或归档；两个接收进程并发时只有一个取得接收权。接收完成前写入的行不出现在业务列表。两站编号或批次名冲突时双方内容都保留，新批次名追加来源站与交接编号；`/handover/imports/{id}` 的「来源对应关系」列出原站编号与本地编号。接收的分发计划一律默认停发，归档下载字节与原站逐字节一致。
两站可分别设置 `SITE_NAME`（默认 `主站`），导出清单与冲突标注会使用该名称。

## 技术与本地开发
Python 3.12 / FastAPI / SQLAlchemy / NumPy / SciPy / scikit-learn；MySQL 8.0；Redis 7；Nginx；原生 HTML CSS JavaScript ES modules（用户指定）。后端所有校验、持久化和算法在服务端。无前端打包步骤。
后端：安装 backend/requirements.txt，设置 DATABASE_URL、REDIS_URL、JWT_SECRET、ARTIFACT_DIR 后在 backend/ 执行 `python -m migrations.initial`、`python -m scripts.seed_demo`、`uvicorn src.main:app`，另开 `python -m src.worker`。前端使用带 /api 反向代理的 Nginx，直接打开 index.html 不支持 API。

## 目录与工程约定
backend/src/{models,schemas,services,routes,controllers,middlewares,constants,config,utils}；backend/scripts/seed_demo.py；backend/migrations/initial.py。
frontend/src/{api,stores,types,components/common,hooks,pages,router,utils,constants}。
Role 枚举：admin/analyst/viewer；JobStatus：queued/running/completed/failed。后端 backend/src/constants/enums.py，前端 frontend/src/constants/enums.js；路由权限在 middlewares/auth.py；页面按相同角色控制操作入口。

## 环境变量与 Docker
COMPOSE_PROJECT_NAME=bulletin-ewma；FRONTEND_PORT=18219；BACKEND_PORT=19219；DB_PORT=17719；DB_PASSWORD、JWT_SECRET 见 .env.example；SEED_DEMO 默认 true。共享命名产物卷供后端下载与 worker 写入，数据库/Redis 各持久卷。
恰好五服务均有健康检查。Redis 队列加 SQL 排队状态兜底；worker 按 next_run 生成本地收件箱文件。周期 60 秒至 30 天。分发结果为批准分析的固定快照；若需新数据重新计算并建立新计划。当前不连接邮件或企业聊天。
错误响应、请求编号、Redis 每 IP 每分钟 240 次限流、JWT 和数据库操作审计。演示密钥与密码仅供本机模拟；生产部署前配置独立密钥与账号。

## 数据与验收
所有数据为固定随机种子生成的模拟业务数据，不能作为实际生产决策证据。项目独立算法、输入边界及业务局限详见 BUSINESS.md。测试与截图统一存放 ../../test-artifacts/gb-1020/，源码不含日志/截图/测试报告。
清理本项目演示数据：`docker compose down -v --remove-orphans`（会删除本项目命名卷）。

## License
MIT，见 LICENSE。
