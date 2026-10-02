# Linux 无桌面开发验收

这是隔离开发工具，不是正式安装器或生产部署方案。完整需求、当前已测架构和未验收项见项目实施记录；不把容器内成功视为 Windows、所有 Linux 发行版或真实抖音登录通过。

## 输入与隔离边界

- 使用 `tools/build_context_package.py` 生成的独立候选 wheel；`tools/stage_linux_acceptance.py` 核对成员白名单、源码/资产完整性及字节一致性。
- 构建上下文只有候选包、自有源码/页面、全部 `test_context*.py` 与候选包白名单测试，以及显式列出的原创验证工具。保留 `backend/{src,tests,tools}` 相对布局；静态 UI 测试仅带入 `frontend/src` 的 JS/JSX/TSX、入口 HTML 和许可说明，不含 `node_modules`、前端环境配置或其他工具。准确文件清单见 `staging-report.json`，不依赖旧的固定测试数。
- 不复制旧 `app`、第三方项目、私人素材库、登录态、模型 Key 或 `.env`；缺少指定工具、链接别名或候选包与源码不一致时，在创建上下文前报错，不能静默减少测试。
- 镜像基于固定摘要的官方 Python 3.12.13 / Debian Bookworm，安装媒体、字体、CPU OCR、MCP、页面和浏览器依赖。构建会访问公共软件仓库；测试运行关闭外部网络，只允许容器内回环的合成 HTTP/页面。
- 正式测试必须使用非 root、只读镜像、禁用新增权限与能力、资源上限；只挂载新建的合成输出目录可写及明确 OCR 模型目录只读。不挂 Docker socket、用户主目录、生产库或真实服务凭据；不发布端口。
- 没有 DISPLAY/GPU/图形钥匙串。无需启动 Xvfb；浏览器只处理本机原创样例。安装浏览器依赖不代表成功续期平台账号。

## 开发操作

在 `backend` 目录使用开发 Python：

```sh
python tools/stage_linux_acceptance.py --wheel <候选wheel绝对路径> --output <库外私有验证目录>
```

根据返回的真实 `context` 路径构建；不要猜临时文件夹名称：

```sh
docker build --platform linux/arm64 -t collection-context-acceptance:linux-arm64 --file <context>/Dockerfile <context>
```

当前 `Dockerfile` 固定摘要对应已检查过的 **ARM64** 基础镜像，不可直接把命令改成 `linux/amd64` 后宣称 x64 可运行。x64 需要另行核对该架构基础镜像摘要、显式设置 runner 的 `--expected-machine x86_64`，并在真实目标环境跑完。runner 会核对实际 Linux/架构；本机 fixture 测试或构建上下文成功都不是 Linux 原生验收。

`Dockerfile` 安装独立候选包；`/acceptance/backend/src` 和 `/acceptance/backend` 只供单元测试定位源码及原创工具，后续 smoke 会清掉 `PYTHONPATH` 并从安装目录运行。当前单元测试上限为 600 秒；它只是运行截止时间，不是性能通过标准。

运行时指定新建空输出目录、三个已核对的 OCR 模型所在目录，以及对输出目录有写权的非 root UID/GID：

```sh
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges --user <非root_UID>:<GID> \
  --cpus 1 --memory 1536m --pids-limit 256 --shm-size 128m \
  --tmpfs /tmp:rw,nosuid,size=512m \
  -e OMP_NUM_THREADS=1 -e OPENBLAS_NUM_THREADS=1 \
  --mount type=bind,source=<新输出绝对目录>,destination=/results \
  --mount type=bind,source=<OCR模型绝对目录>,destination=/models,readonly \
  collection-context-acceptance:linux-arm64
```

这只是验收容器，无外网，不能用它同步真实来源或请求云模型。容器正常结束会移除，输出目录保留；失败报告也保留，不覆盖旧结果或通过忽略失败继续宣称成功。

### Docker 虚拟机没有共享宿主路径时

不要通过增加用户主目录/外置盘的宽泛共享来解决验收挂载。使用独立命名卷，给资源采用本次独有的名称；准备容器只挂两个新卷，不挂生产路径或 Docker socket。

1. 建立输出/模型两个新卷，创建一次性准备容器；仍断网、只读镜像、禁用全部能力，仅为输出卷初始化保留 `CHOWN`。准备命令先把卷目录权限设为700，再把所有者改为实际非root测试UID/GID；换所有者后不要再让缺少`FOWNER`的root执行chmod。
2. 通过 `docker cp` 复制 `PP-OCRv6_det_small.onnx`、`PP-OCRv6_rec_small.onnx`、`ch_ppocr_mobile_v2.0_cls_mobile.onnx` 三个明确文件，不复制父目录或凭据。模型卷目录/文件只需非root可读，运行时按只读卷挂载。
3. 检查准备容器实际退出码为0及输出卷UID/权限；若初始化失败先定位。实际测试容器保留到取回报告，先不用`--rm`。沿用上述断网、非root、只读根、资源限制；将两个bind替换为对应volume挂载。
4. 测试容器终止后用 `docker cp <本次测试容器>:/results/. <新建宿主输出目录>` 取回，核对总报告/分步报告；随后只清理已确认属于本次的容器与测试卷。不运行全局prune，不删除其他项目资源。

这一路径不改变现有Colima/虚拟机共享配置。准备失败、测试失败及成功结果分别记录，不因宿主路径不存在或权限错误就宣称Linux功能不可用或已通过。

## 证据

`linux-report.json` 记录实际系统/架构/Python/UID、依赖与每步退出状态；分步日志、合成库、截图、媒体基准与其报告保留在输出目录。

单元测试同时输出 `pytest-junit.xml`，总报告按 case 统计总数、实际运行、通过、失败、错误和跳过，并逐项保留跳过原因。Linux 可适用的本机媒体、TLS、独立浏览器锁和页面验收通过显式 `RUN_LOCAL_*` 开关运行；浏览器路径由已安装的固定版 Playwright SDK 解析并核对，不隐式下载。缺少 FFmpeg/FFprobe/OpenSSL/Chromium 或出现未预期跳过，都不能凭 pytest 退出 0 算通过。Mac Keychain 测试在 Linux 属于明确不适用；这不意味着 Linux 系统凭据能力已经完成。既有用例内部的 3 秒等待标准不变。

`web_smoke.py` 的 `--browser-binary` 仅用于显式选择已安装的开发浏览器，不携带登录资料；成功与失败报告、失败截图都写入本次新建的合成目录。本机开发浏览器通过不算 Linux 容器/目标架构已通过。

依次检查：新核心单元测试 → 安装后搜索/读取 → 写事务中断 → 三个只读 MCP 工具 → 真实回环服务/无桌面浏览器页面 → 三角色本机合成 HTTP/持久续跑 → 10段视频/50页 CPU OCR 基准 → 本产品独立浏览器生命周期。任一步失败则停止并报告失败，不把后续未跑项视为成功。

单元测试会包含链接/分页替身，提取测试会产生本机合成请求；这都不是实际抖音同步、真实云 ASR/视觉精度或宿主 AI 问答。测试只结束自建子进程，不能根据此脚本重启或清理其他项目。
