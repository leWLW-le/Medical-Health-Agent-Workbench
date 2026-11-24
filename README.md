[English](README.en.md) | **简体中文**

# Medical Report Audit Agent

基于开源 [Smart Health Assistant](https://github.com/wananing/Smart-Health-Assistant) MIT 源码深度二次开发的**检验报告核对与证据问答平台**。项目引入账号体系，重构前端入口与会话管理，并将检验报告纳入可追溯的审核流程。上游基线为 `24f749b`。

## 主项目功能

- **登录与账号工作区**。健康对话、文字预问诊及药管家支持独立对话历史。报告内容解析按账号保存报告结果、手动校正和追问。
- **报告解读**。PDF 和照片共用一个导入入口，使用本地 OCR 提取检验表。按报告自带的参考范围整理结果，待确认字段注明原因，可定位原页后校正。
- **患者版体检解读单**。上传后先展示偏高、偏低和待核对项目，再通过对话补充身体情况。解读附有原页依据，并提供就诊准备和紧急程度提示。
- **指标说明与追问**。根据识别通过或人工校正的字段解释指标、结果和参考范围，并注明原报告页码。合成评测覆盖提取、异常漏检、原页定位和证据引用。
- **异步审核演示**。模拟病历的批量审核、版本复核和证据问答可在独立演示入口体验。

演示使用合成病历。

## 运行方式

| 模式 | 入口和依赖 | 用途 |
| --- | --- | --- |
| **账号主版（默认）** | React/Vite → Spring Boot → Python Agent | 登录后使用报告内容解析、异步核对和账号历史。 |
| 单机调试版 | 显式设置 `VITE_AUDIT_ASYNC=0`、`VITE_MANAGED_HEALTH_CHAT=0`，前端直连 FastAPI | 独立调试导入、OCR 和核对规则。 |

账号主版可通过 Docker Compose 或本地 H2 启动。服务就绪后，在健康助手里打开 [报告内容解析](http://127.0.0.1:5173/#/report-audit)。 [账号版启动说明](APPLICATION.md)

## 技术架构

- **前端**使用 React、TypeScript、Vite 和 Tailwind CSS。通过同源 `/api/health` 访问账号网关，使用 SSE 接收文字流与结构化卡片。
- **应用层**使用 Java 21、Spring Boot、Flyway、MySQL 和 Redis。负责会话、所有权校验、账号历史、任务队列与版本管理。无容器调试可使用 H2。
- **智能体与核对规则**使用 Python、FastAPI、LangGraph、LangChain 和 Pydantic。OCR 使用 PDFium 与 RapidOCR，文档解析使用 pypdf、openpyxl 和 xlrd。核对结论来自确定性规则，可选接入 MCP 和混合检索。

## 验证和开发文档

- [报告工作台、单机调试和 OCR 评测](REPORT_AUDIT.md)
- [应用层、账号、异步任务和配置](APPLICATION.md)
- [LangGraph 运行时](docs/langgraph-runtime.md) · [可观测与 Agent 评估](docs/observability-evals.md)

## 上游署名与许可

本项目基于 Smart Health Assistant 的 MIT 许可源码。上游署名和许可见 [LICENSE](LICENSE)。
