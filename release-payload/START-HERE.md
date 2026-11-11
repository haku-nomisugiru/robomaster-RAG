# RoboMaster CAD RAG v0.5.0 Windows x64

1. 解压整个 ZIP 到普通可写目录。
2. 双击 `start-rag.cmd`。窗口出现 `Listening on http://127.0.0.1:8765` 后，打开 [健康检查](http://127.0.0.1:8765/health)。
3. 保持启动窗口打开。结束时按 Ctrl+C。

包内提供 Python、SQLite 文本与几何索引、3,468 条 Chroma 向量和查询编码器。首次功能查询会载入本地模型，并为本次查询生成向量。现有卡片向量直接从 Chroma 读取。

- 型号精确查询示例 `http://127.0.0.1:8765/v1/search?q=3508&mode=model`。
- 功能查询使用 `mode=function`。响应中的 `retrieval` 为 `hybrid` 时，结果可通过 `retrieval_channels` 检查 SQLite 和 Chroma 的召回。
- 默认端口被占用时，可在该目录终端运行 `start-rag.cmd --port 8766`。
- 几何索引随包交付。几何相似检索会校验来源 CAD 与测量 STEP，按卡片的原帖、相对路径和 SHA-256 补齐文件后使用。第三方 CAD 由原作者提供。

服务只绑定本机地址。接口和复用示例见 [HTTP 服务说明](reports/HTTP_SERVICE.md)。完整来源、发布基线和依赖版本见 `release-manifest.json` 及 `_runtime/`。
