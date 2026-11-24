# 检验报告异步审核与证据问答平台

这是基于 [Smart Health Assistant](https://github.com/wananing/Smart-Health-Assistant) MIT 源码深度二次开发的账号主版。项目重新设计了前端入口、账号会话和报告审核流程。应用层使用 Java 21 和 Spring Boot，智能体服务使用 Python、FastAPI、LangGraph 和 LangChain。项目使用内置模拟病历。

| 组件 | 责任 |
| --- | --- |
| React 工作台 | 批量上传、任务进度、报告更正、版本回看和证据问答 |
| Spring Boot API | 任务提交、查询、版本约束和问答代理 |
| Java Worker | 从任务表领取工作，调用 Python 审核，保存结果、重试或标记失败 |
| MySQL 与 Flyway | 保存报告、不可变版本快照、任务租约和幂等请求 |
| Redis | 按报告与版本缓存已完成结果，通过 Lua 按来源 IP 限制问答请求 |
| FastAPI 与 LangGraph | 解析模板报告、确定性字段核对、增量复核，路由到病历证据或知识检索节点 |
| MCP 知识服务 | 通过 Streamable HTTP 提供检验术语检索。可调用 BM25、向量 MMR 和 RRF，返回资料出处 |

提交时，MySQL 在一个事务内写入幂等键、报告与任务。重试同一请求可使用原 `Idempotency-Key`，相同键和请求体会拿到原任务。同一报告的更正用已完成版本做条件更新。任务与报告结果的成功状态在同一个事务内提交。

Worker 使用 `FOR UPDATE SKIP LOCKED` 领取任务，短事务提交后再调用 Python。运行中的任务有 45 秒租约。进程退出后其他 Worker 可以重新领取，完成时检查租约与领取令牌。重试可能再次执行审核，同一报告版本只保存一份结果。Python 调用超时或熔断时任务会退避重试。问答使用独立熔断器与并发上限，异常期间报告历史仍可查询。

Redis 缓存键包含报告版本，查询最新版本时先读 MySQL 的已完成版本号。Redis 不可用时读取 MySQL，并继续由本机并发上限保护问答。并发上限是每个 Java 实例 8 个问答调用，分布式问答限流默认每 IP 30 次每分钟。

## 用 Docker Compose 启动服务

在仓库根目录设置以下环境变量，再启动容器。以下命令生成只用于本机演示的随机值。若重用 MySQL 卷，应继续使用首次初始化时的密码。

```powershell
$env:AUDIT_AGENT_TOKEN = [guid]::NewGuid().ToString('N')
$env:AUDIT_DB_PASSWORD = [guid]::NewGuid().ToString('N')
$env:AUDIT_MYSQL_ROOT_PASSWORD = [guid]::NewGuid().ToString('N')
docker compose -f compose.audit.yml up --build -d
```

另一个终端启动前端。

```powershell
cd frontend
npm ci
npm run dev -- --host 127.0.0.1
```

「健康助手」里的「报告内容解析」提供统一报告解读。它通过同源 `/api/health/report-reading` 访问 Java 网关。Flyway V6 增加按账号保存的报告解读记录。Vite 的 `HEALTH_GATEWAY_PROXY` 可指定网关地址。部署构建产物时，将整个 `/api/health` 路径转发到 Java，并关闭 SSE 响应缓冲。

报告内容解析与健康对话、AI 诊室和药管家共用 `main:app` 中的 LangGraph 主图。Java 网关将单项解读和报告追问交给健康服务。主图将结构化报告请求分派到报告解读流程，处理整份报告、单项和关联项。核对服务负责文件导入、OCR、原页渲染和字段比对。在当前账号的同一份报告内，已保存的问答可用于恢复上一轮话题。检验结果和页码取自已确认字段。医学资料使用内置的 MedlinePlus 等来源摘要，先按指标别名筛选，再用 BM25 排序。运行环境已有本地 BGE 缓存时，模型解读增加向量检索和 RRF 排序。覆盖范围见 `backend/report_audit/reading_knowledge.py`。

默认使用模型解读。报告助手在核对报告和已有回复后，根据本次问题补问，每次一个问题，最多三次。用户可以直接回复、跳过、要求先给结论或另问报告。LangGraph 使用 SQLite 保存等待状态，账号网关把断点与该报告的对话一起保存。重开报告后，回复会恢复同一任务并复用报告字段版本一致的证据。校正字段后，页面会按新版本重查最近的问题。报告查询、资料检索和联算结果进入模型的下一轮请求。段落通过校验后流式显示，回答附带证据、缺失的信息和工作记录。待确认项与紧急提醒按规则处理，模型超时仍可根据已核验的证据答复。

健康服务默认将报告解读断点存入 `backend/report-reading-checkpoints.sqlite`。若原来在核对服务上设置过 `AUDIT_READING_CHECKPOINT_DB`，请在启动 `main:app` 时继续使用同一文件路径。Compose 将已有的 `reading-checkpoints` 卷挂载到健康服务。要为报告解读单独配置模型，可以在健康服务上设置 `REPORT_READING_PROVIDER`、`REPORT_READING_MODEL`、`REPORT_READING_API_KEY` 和 `REPORT_READING_BASE_URL`。可在 `backend` 运行 `uv run python -m unittest test_report_reasoning.py test_report_reading_task.py` 验证多轮恢复、工具调用和回退。

报告解析与其他咨询模式共用历史侧栏、消息样式和底部输入区。用输入区的「上传报告」开始，点「查看报告」打开检验摘要和字段校正，也可以从回答直接跳到原页。解读常见血常规异常时，程序先检查所需的关联字段。已有唯一确认结果时直接复用，否则查找本报告识别出的字段。缺失、待核对或有多条采样记录时，结果会带入解读并显示在工作记录中。程序查证与模型工具轮数分别记录。

血常规解读可以展示指标间的数值核验，用血小板计数与平均体积估算血小板压积，并对照白细胞比例、绝对计数和总数。系统对可唯一配对的同页记录给出同次采样条件、单位假设、显示精度和原页依据。模型可使用核验结论补充解读。

在 [报告内容解析](http://127.0.0.1:5173/#/report-audit) 上传 PDF 或照片，查看摘要、校正和指标说明。要体验模拟病历的异步审核，打开 [模拟核对入口](http://127.0.0.1:5173/#/report-audit-demo) 并填入示例报告。血红蛋白示例为报告值 123 g/L、原始病历 132 g/L。采用原始记录提交更正后，新版本只复核 1 项，沿用另外 2 项。

### 报告更正影响追踪

在示例报告 v1 中分别询问血红蛋白和白细胞的原始记录，然后采用血红蛋白的原始记录提交更正。在新版本的「更正影响」中可以看到血红蛋白答复的复核任务、新旧字段与原答复。白细胞答复仍可在「证据问答」查看。复核时可按更正后的版本重新提问，并在人工确认后将任务标记为已复核。

LangGraph 在生成证据答复后记录所引用的报告字段。Java 按字段比较每次发布前后的快照，并在发布事务内为受影响的答复创建任务。同一答复针对同一更正版本只有一条任务。针对旧版本提问时，系统检查随后发布的更正，覆盖问答与更正并发完成的情况。Flyway V3 保存问答、字段引用和复核状态。此前仅存在页面中的临时问答无法回溯。

默认 MCP 返回本地术语说明。启动 Compose 前设置 `REPORT_AUDIT_KNOWLEDGE=1` 可开启混合检索。首次检索需要访问公开模型仓库下载嵌入权重。设置 `REPORT_AUDIT_LLM_ROUTER=1` 与模型环境变量，可在规则无法确定意图时用 LangChain 请求结构化分类。分类只用于选择问答路径，核对结果由规则产生。账号前端通过 8080 端口访问 Java，Java 分别调用检验核对和健康对话的 Python 服务。

## 不使用容器启动账号主版

本机可用 Java 21、Maven、uv 和 Node.js 运行。在不同终端启动以下服务。为两个 Python 服务各选一个本机服务凭据，并在 Java 终端填写对应的同一值。

```powershell
# 检验核对 Agent
cd backend
uv sync --locked
$env:AUDIT_AGENT_TOKEN = '<核对服务凭据>'
uv run uvicorn report_audit.agent_service:app --host 127.0.0.1 --port 8091
```

```powershell
# 健康助手主图与报告解读
cd backend
$env:HEALTH_AGENT_TOKEN = '<健康服务凭据>'
$env:RAG_WARMUP = '0'
uv run uvicorn main:app --host 127.0.0.1 --port 8000
```

```powershell
# Java 应用与同进程 Worker，使用本地 H2
cd application
$env:SPRING_PROFILES_ACTIVE = 'local'
$env:AUDIT_AGENT_TOKEN = '<核对服务凭据>'
$env:AUDIT_AGENT_URL = 'http://127.0.0.1:8091'
$env:HEALTH_AGENT_TOKEN = '<健康服务凭据>'
$env:HEALTH_AGENT_URL = 'http://127.0.0.1:8000'
mvn spring-boot:run
```

```powershell
cd frontend
npm ci
npm run dev -- --host 127.0.0.1
```

访问 [报告内容解析](http://127.0.0.1:5173/#/report-audit)，登录后可查看报告。异步核对的模拟流程见上文。可选 MCP 知识服务用 `uv run uvicorn report_audit.mcp_server:app --host 127.0.0.1 --port 8092` 启动，并为检验核对 Agent 设置 `AUDIT_MCP_URL=http://127.0.0.1:8092/mcp`。

Java 测试在 `application` 运行 `mvn test`，默认使用 H2。也可把 `AUDIT_TEST_JDBC_URL`、`AUDIT_TEST_DB_USER` 和 `AUDIT_TEST_DB_PASSWORD` 指向空的 MySQL **测试专用库**。测试会清理该库的审核表。Python 回归在 `backend` 运行 `uv run python -m unittest test_report_audit.py`。前后端启动后，在 `frontend` 运行 `node test_report_audit.cjs` 验证账号模式。

## 账号文字对话

前端默认启用健康对话、文字预问诊及药管家的账号模式。核心请求经过同源 `/api/health`，Vite 开发环境代理到 Java 的 8080 端口。可用 `HEALTH_GATEWAY_PROXY` 指定其他地址。部署构建产物时，把同一路径转发到 Java，并关闭该路径的 SSE 响应缓冲。输入框的「上传图片」支持药盒和追溯码识别，识别时将图片提交给配置的视觉模型，账号历史保存提示文字与回复。菜单内的「报告照片解读」进入报告内容解析。

Spring Boot 管理账号、口令哈希、登录会话及对话归属。每个账号可在三个模块分别新建多条会话，左侧按最近活动排序，切换后可加载选中会话的最近 50 轮。Flyway V4 将已有记录保留在对应模块的原会话中，并为新对话分配独立编号。浏览器使用 HttpOnly 会话 Cookie，部署到 HTTPS 时设置 `HEALTH_SECURE_COOKIE=true`。退出登录撤销当前会话，同一账号可以在另一个浏览器登录。单机模式以浏览器保存文字记录，账号模式使用数据库记录。

每轮先将用户提问写入数据库，再通过 SSE 转发 Agent 事件。只有 Agent 发出完成事件并成功提交回复后，Java 才向前端确认完成。请求编号用于去重，同一会话的并发写入受两分钟数据库租约保护。Redis 对登录和注册按来源地址限流，文字对话按账号限流。Java 的虚拟线程承载流式连接，每实例默认允许 8 个活跃 Agent 请求。熔断或断流期间，用户仍可查询或删除历史，已经保存的提问会保留。

Python 的 LangGraph 主图处理预问诊、用药、健康顾问和报告解读。仅内部调用使用 `HEALTH_AGENT_TOKEN`，Compose 默认复用 `AUDIT_AGENT_TOKEN`。开发模式下启动 `main:app` 与 Java 前，将两边的 `HEALTH_AGENT_TOKEN` 设为同一值。Java 默认访问本机 8000，可通过 `HEALTH_AGENT_URL` 调整。文字对话可在 Python 进程内继续尚未结束的追问。进程重启后，Java 会把该对话的最近四轮完整问答重放给 Agent。删除记录时，Java 清除应用历史，并请求 Agent 清理对应的临时状态。

本体验环境使用合成数据。发布给真实用户前，需要根据部署环境落实医疗健康数据的保留期限、删除、访问审计以及第三方模型的数据处理要求。

「查医保」演示入口已移除。当前项目未接入个人医保账户接口，原医保对话路由只提供国家医保服务平台的登录指引。

## 无外网时使用

「检验核对」使用合成病历和本地规则。Java、数据库、Worker 和 Python Agent 已在本机运行时，上传模板报告、差异核对、更正与证据问答可以继续使用。术语查询默认使用本地说明，混合检索需要提前下载本地嵌入权重。

首页对话和报告内容解析通过 `main:app` 运行。在 `backend` 运行以下命令。此步骤假设项目依赖已安装。

```powershell
$env:RAG_WARMUP = '0'
uv run --no-sync uvicorn main:app --host 127.0.0.1 --port 8000
```

如要离线使用首页对话，先联网下载支持工具调用的模型到 Ollama 等本地运行器，然后在启动 Python 服务前设置以下环境变量。拍照解读还需要提前下载兼容的视觉模型，并设置 `VISION_MODEL`。

```powershell
$env:LLM_PROVIDER = 'custom'
$env:LLM_BASE_URL = 'http://127.0.0.1:11434/v1'
$env:LLM_API_KEY = 'ollama'
$env:LLM_MODEL = '<ollama list 中已下载的聊天模型>'
```

健康 Agent 使用不同地址时，设置 Java 的 `HEALTH_AGENT_URL` 并重启网关。Ollama 的本地兼容接口说明见 [官方文档](https://docs.ollama.com/api/openai-compatibility#local-server-usage)。

## 接口与数据范围

- `POST /api/report-audit/cases` 与 `POST /api/report-audit/batches` 返回 HTTP 202 和任务数组。批次接收 `{"drafts": [...]}`，每批至多 25 份。文件先通过上传接口解析为结构化报告。
- `GET /api/report-audit/jobs/{id}` 查询任务状态，`POST /api/report-audit/jobs/{id}/retry` 重试可恢复失败。
- `POST /api/report-audit/cases/{id}/revisions` 用 `expected_version` 提交更正。历史版本可用 `GET /api/report-audit/cases/{id}?version=1` 查询。
- Java 模式按账号保存最近一次导入的结构化草稿或扫描预览。手动编辑可通过「保存草稿」保存。提交后的任务与更正版本可从「核对历史」恢复，无需保留原浏览器标签页。系统保留解析后的数据，原上传附件不入库。
- Flyway V5 为新核对任务记录所属账号。旧演示记录没有账号归属，保留在原表中，不进入账号历史。
