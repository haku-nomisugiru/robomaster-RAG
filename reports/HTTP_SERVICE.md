# 本机 HTTP 检索服务

服务地址：http://127.0.0.1:8765 。只监听本机回环地址，为桌面插件及本机AI工具提供只读查询。实现使用Python标准库，无新增运行依赖；这是一项本机后台进程部署，未注册Windows系统服务，也未设置开机启动。

## 启停

在仓库根目录创建 .venv，执行 python tools/library.py index 后运行下列命令。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/start-service.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/status-service.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/stop-service.ps1
```

日志为 runtime/stdout.log、runtime/stderr.log，进程记录为 runtime/service.json。启动脚本遇到占用端口会报错，不替换现有服务。停止脚本核对记录的进程启动时间及命令行后停止服务。更新样本库时先停止服务，卡片更新后运行 tools/library.py index。完整重新入库后运行 build 和 validate，再启动服务。

## 接口

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | /health | 健康与样本数量 |
| GET | /openapi.json | AI及客户端接口描述 |
| GET | /v1/sections | 三类名称及数量 |
| GET | /v1/search?q=关键词&section=分类&limit=5 | 关键词检索，返回完整卡片 |
| GET | /v1/models/{model_id} | 模型详情、来源、验证状态、下载链接 |
| GET / HEAD | /v1/models/{model_id}/file | CAD入口文件 |
| GET / HEAD | /v1/models/{model_id}/package | 来源模型包（原始ZIP或STEP等） |

section取gimbal（云台）、chassis（底盘）、wheel_or_leg（轮子或轮腿）。省略分类会检索全部26张卡片，包括8张补充资料；三类主库分别为3、5、10张。q长1–256字符，limit为1–50。无命中返回200及空items。未知ID返回404，参数错误400，本地文件缺失返回404，文件内容与登记SHA-256不符返回409。

响应中的 source/source_url 指向原帖，entry记录CAD路径与SHA-256，verification记录解析/运动/配合等状态，dependency_scope记录已保存引用检查结果，limitations记录限制。engineering_parameters中的null表示未验证，不能当作0。downloads.model和downloads.package是相对于服务地址的路径，package_sha256用于校验原始包。下载响应也包含X-Content-SHA256及ETag。

## 插件调用

```powershell
Invoke-RestMethod 'http://127.0.0.1:8765/health'
$q = [uri]::EscapeDataString('舵轮')
$result = Invoke-RestMethod "http://127.0.0.1:8765/v1/search?q=$q&section=wheel_or_leg&limit=3"
$result.items | Select-Object id,name,source_url
```

C#示例见 examples/CadLibraryClient.cs，引用System.Net.Http。先SearchAsync，再GetModelAsync，读取详情中的哈希后调用DownloadAsync。JSON解析可使用现有插件依赖。下载采用流式写入临时文件、客户端SHA-256校验后再落盘，不覆盖已有目标文件。示例未加入现有SolidWorks插件工程，也未在其目标框架内编译验证。

原生SLDASM应优先取package以保留包内相对目录；仅下载入口SLDASM通常不足以打开装配。package是原始来源包，并不保证所有依赖已齐全。STEP子定义仍返回完整STEP，客户端需按definition_id定位。此服务不启动SolidWorks、不修改配合、不自动插入装配。

## AI调用

本机AI工具可以读取 /openapi.json，也可包装 examples/search_models.py 的search_models函数作为工具。推荐流程：按功能检索→读取候选详情→将来源、验证状态和限制交给AI→选择模型→下载校验。AI不得将作者陈述视作本地实测，不得将未知接口当作兼容，不得将文件可下载解释为可直接装配。

远端AI无法直接访问用户电脑的127.0.0.1；应由本机插件或本机工具执行HTTP请求，再把结果传给AI。接口拒绝带Origin的浏览器跨域请求；WebView应通过插件宿主调用服务。此版本不支持局域网访问、向量检索、断点续传或自动解包。每次下载前完整计算SHA-256，大文件可能有等待时间。勿在下载过程中修改原资产。
