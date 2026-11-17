# 日切放行

> 遵守 `ops/底座生成前必读_效率与技术栈.md`。FastAPI + Vue 3 + SQLite，交付须 docker compose 一键起；库用 SQLite，不挂外置 Postgres。  
> **禁止同构换皮**：本仓主链路、表结构、接口与页面均独立，不得与其它仓共用同一套 `/api/items` 壳。

## 骨架一句话

业务日分区切片登记后裁决完整度，完整才放行下游，并与消费水位对账

## 基本信息

| 项 | 值 |
|----|-----|
| 产品名（首页） | 日切放行 |
| 目录 | `04-slicegate` |
| 领域建议 | 数仓与数据湖 |
| 接口 | http://127.0.0.1:10640 |
| 库 | `data/app.db` |
| 账号 | admin / 123456 |

## 为何能出多题

迟到切片、重切、跨分区依赖、部分完整冻结、消费水位回拨

## 避开

别做成作业拓扑编排换皮；别做成纯水位大盘

## 互斥切口候选

- 迟到切片整批拒收 vs 进待补齐冻结区
- 重切失败整笔回滚 vs 保留已完整分区
- 放行与水位同步闸 vs 异步放行后纠偏

## 启动（一键）

`powershell
cd projects/04-slicegate
docker compose up -d --build
`

浏览器：http://127.0.0.1:10640/  
健康检查：GET http://127.0.0.1:10640/api/health

本机开发（可选）：

`powershell
python -m pip install -r requirements.txt
python -m uvicorn app:app --app-dir backend --host 127.0.0.1 --port 10601
python -m pytest -q
`

## 日界锁定（甲方案钉死）

口径权威文档：[`docs/日界锁定说明.md`](docs/日界锁定说明.md)，运行时经
`GET /api/boundary/spec` 读取同一份说明。选定后当天不得更换，代码中无切换写路径。

- **甲方案**：日切在同一事务内作废昨日全部生效锁定，次日必须重新钉策略；
  乙方案显式结转未启用，结转/回拨等偏离动作一律拒绝并登记「日界口径偏差」。
- 放行服务只承认当前业务日的生效锁；跨日残留锁当场作废清理并审计，不参与拦截。
- 日切与策略钉死互斥（进程锁 + `BEGIN IMMEDIATE`），后手登记「日界争用」并返回 409。
- 策略台、成功流水、关日台、详情旁注、目录旁注五处读数同源；任何日界变更必有审计行。

| 接口 | 说明 |
|------|------|
| `GET /api/boundary/spec` | 钉死口径与规则 |
| `GET /api/boundary/state` | 策略台/关日台同源读数（当前业务日、锁、日切、偏差/争用计数） |
| `POST /api/slices/{id}/lock` | 钉策略锁（biz_date 取当前业务日，与日切互斥） |
| `POST /api/boundary/locks/{id}/void` | 当日人工解锁（留审计） |
| `POST /api/boundary/carryover/{id}` | 乙方案结转入口：拒绝并登记偏差 |
| `POST /api/boundary/day-cut` | 日切：推进业务日并同事务作废生效锁 |
| `GET /api/boundary/audit` | 日界审计流水 |
| `GET /api/slices/{id}/boundary` | 详情旁注（与目录旁注同一构造函数） |

新增表均在本机 `data/app.db` 内：`biz_day_state`、`boundary_config`、
`policy_locks`、`day_cuts`、`boundary_audit`。会话登录与单页站点不变。
