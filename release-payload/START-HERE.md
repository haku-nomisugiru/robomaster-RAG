# RoboMaster CAD RAG v0.5.1 Windows x64

1. 解压整个 ZIP 到普通可写目录。
2. 双击 `start-rag.cmd`。窗口出现 `Listening on` 后，按显示的地址访问 `/health`。默认地址是 [本机健康检查](http://127.0.0.1:8765/health)。
3. 保持启动窗口打开。结束时按 Ctrl+C。

包内提供 Python、SQLite 文本与几何索引、3,468 条 Chroma 向量和查询编码器。首次功能查询会载入本地模型，并为本次查询生成向量。现有卡片向量直接从 Chroma 读取。

- 型号精确查询示例 `http://127.0.0.1:8765/v1/search?q=3508&mode=model`。
- 功能查询使用 `mode=function`。响应中的 `retrieval` 为 `hybrid` 时，结果可通过 `retrieval_channels` 检查 SQLite 和 Chroma 的召回。
- 默认端口被占用时，双击入口自动选择空闲端口。也可运行 `start-rag.cmd --port 8766` 显式指定端口。
- 打开检索结果的 `reuse_href` 查看获取状态，再在本目录运行 `get-cad.cmd <卡片ID>`。已有作者附件、CAD 或解压目录时加 `--from "路径"`。命令核对文件指纹并按卡片路径安装，直接使用现有索引。第三方 CAD 由原作者提供。
- 几何相似检索会校验来源 CAD 与测量 STEP，原生 CAD 用于量测的 STEP 导出需要按卡片记录另行准备。

服务只绑定本机地址。RoboMaster 工作台的配置和复用示例见 [README](README.md#接入-robomaster-机械设计工作台) 和 [HTTP 服务说明](reports/HTTP_SERVICE.md)。
