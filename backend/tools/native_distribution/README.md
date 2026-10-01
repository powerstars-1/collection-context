# 原生基础候选构建（尚非完整桌面发行）

本工具仅制作当前构建系统的 PyInstaller `onedir` 候选：目录中包含 Python 和管理页静态资源，新用户不必另装 Python 或 Node。当前计划实测 macOS ARM64；在 Mac 构建不代表 Windows 或 Linux 已经支持。

源码入口为同一个 `CollectionContext` 程序的 `cli`、`mcp`、`web`、`launch`、`desktop` 子模式，分别直接进入现有业务入口，没有 shell 中转或任意程序执行。控制台程序无参数只显示说明，不创建库、不打开网页、不创建权限。

`build.py --output <库外专用目录> --desktop-app` 在当前 Mac 生成独立的 `CollectionContextDesktop.app`，同时保留控制台程序供 CLI 和 stdio MCP 使用；不把无控制台程序冒充 MCP 可执行文件。`.app` 无参数只打开选库窗口，不自动建立资料库或后台服务。其他系统的桌面打包当前明确拒绝，不能用 Mac 构建结果代替其原生验收。

## 构建边界

- 构建环境新建在仓库外；`requirements-build.in` 锁定五个直接依赖（含Playwright1.63.0），39个传递构建依赖由完整hash锁固定。只使用PyPI与官方组件，不安装参考项目。
- `build.py --output <库外专用构建目录>` 仅复制自有 `src/collection_context` 与静态资产，拒绝源码链接和未列入白名单的类型；新建唯一临时 stage，不覆盖已有发行目录。
- 不自动下载依赖、不使用开发者证书签名、公证、发布或配置常驻服务。macOS ARM64 的 PyInstaller 会自动生成本机运行所需的 ad-hoc 签名；它不是 Apple 开发者签名或公证。构建者需先在隔离环境安装依赖，然后用该环境 Python 执行工具。
- 基础包包含Playwright SDK及其wheel内Node／JS，使用官方hook，不读取宿主Node或浏览器缓存；构建前拒绝任何 `.local-browsers`、链接或未注册资源。包不含Chromium浏览器、FFmpeg或OCR运行库／权重；无法宣称完整同步与识别均开箱即用。用户模型账户与费用另行配置。
- PyInstaller 许可有针对打包应用的例外，但 Python、GUI 库和所有传递依赖仍须保留各自许可。当前候选尚不能替代公开发行许可审查。
- `licenses.py` 只读取显式构建环境的已登记分发元数据、许可原文和准确注册的前端声明；不导入依赖或扫描用户资料。拒绝链接、硬链接、身份冲突、非空输出与超限读取，记录版本、原文字节哈希和来源相对路径。清单只代表构建环境，不断言每个包都在运行时内；CPython、Tcl/Tk、OpenSSL、原生嵌入组件以及产品许可仍须独立闭合。`runtime_sbom_verified` 固定为 false，不因收齐 dist-info 文件而宣称发行许可完成。

官方资料：[使用与 onedir 参数](https://pyinstaller.org/en/stable/usage.html)、[许可与例外](https://pyinstaller.org/en/stable/license.html)。

## 首次口令与无控制台窗口

`native_bootstrap.present_owner_token` 使用本机 Tk 窗口直接展示新产品口令：用户明确勾选已自行保存后返回 True，关闭或取消为 False；GUI 故障只返回脱敏错误。口令使用可选择的只读输入框，并提供明确的“复制口令到本机剪贴板”按钮；展示、确认和关闭不会自动复制或清空用户剪贴板。界面提示剪贴板历史、跨设备同步和其他应用读取的风险，复制不等于已保存。没有命令行参数、环境变量、文件、日志或 URL 的秘密传递。清理内存仅尽力而为，不承诺字符串零化或防止截图。窗口构造途中失败也清理已建立的 Tk 根窗口。

共享启动器已接收 `owner_presenter` 和 `stop_event`。桌面控制器在工作线程调用共享启动器，以进程内队列把首次口令请求交给 GUI 主线程；确认后才继续服务。取消和异常撤销本次新建身份，已有口令不改动、不重新展示。关闭或停止先解锁待确认回调，再通知共享启动器有序退出。本工具不会先固定一个口令以规避这个契约。口令窗口必须在 GUI 主线程执行；跨线程会拒绝。

`python -m collection_context.native_bootstrap desktop` 明确打开选库与启动窗口，可选 `--workspace`、`--port`；无参数的 bootstrap 仍只显示帮助。选择路径和显示依赖是只读的，只有用户点启动才调用共享启动器。空库初始化须勾选且再次确认；非空未知目录拒绝覆盖。窗口显示浏览器/FFmpeg/OCR 降级，包含停止与退出状态；关闭时保持窗口直到服务实际退出，不自动同步、调用模型或安装。错误只展示固定文案，不复述内部异常。

2026-10-01 已实际生成新的 Mac ARM64 控制台程序与无控制台 `.app`。新控制台在中文／空格异路径、空 PATH 和新 HOME 下通过 CLI、stdio MCP、认证 HTTP、终端启动及 SIGTERM 退出验证。首份GUI实测发现 macOS 外置盘授权待确认会卡住界面主线程；其后已将全部只读诊断移至单个后台线程，按参数代次拒绝旧结果。停止／退出只作废结果，不谎称取消已进入的系统调用，不等待只读诊断线程；服务仍保持有序停止。

修复后重新构建在内置磁盘，实际选择原创样例库并输入18798，窗口显示就绪、HTTP健康正常且只监听本机；停止后状态恢复，退出后本应用PID和监听均消失。没有初始化新库、平台或模型请求。此项证明本机GUI普通启动／停止／退出，不代替等待系统授权时的实机响应、首次口令复制、浏览器手动登录、新用户、三端安装或识别质量。详细候选与证据路径见项目实际验收记录。

无 Tk 或无显示环境应明确提示 `desktop_display_required`，服务器使用 CLI/MCP/web，不强制桌面模式。新的 `.app` 是开发候选，未做开发者签名、公证、常驻或跨平台实机验证，不得称完整桌面发行已完成。

## 模型凭据后端

Mac 桌面候选明确启用配置后，默认使用系统钥匙串；库外 `credentials-system` 目录只保存产品命名空间与确认引用，不保存模型 Key 正文。系统锁定、拒绝、不可用或目录身份不符时明确失败，不自动改存文件。普通读库启动不创建系统条目、不读取已有 Key，也不迁移旧 `credentials`。配置与执行授权仍是两个独立开关。

CLI／HTTP／后台的本机启动者可明确选择 `--credential-backend system`，必须使用同一系统凭据目录。服务器保留默认 `private-file` 模式用于权限受限的独立服务目录，页面明确显示未加密；它不是 system 失败时的自动回退。当前系统后端只实现 macOS，Windows/Linux 指定 system 明确拒绝，仍需平台实现及原生验收。

只在新的隔离产品命名空间使用无效合成测试字符串；真实测试不枚举系统库、不读取已有 Key。源码原生读写通过不等于打包程序或升级后的签名访问已通过，各自证据见实施记录。测试工具不得把 Key 置于 argv、环境、日志或公开资料中，也不得根据遗留文件猜测删除系统条目。

`system_credentials_smoke.py --binary <明确的Mac控制台> --output <不存在的新库外目录>` 仅测试原创图文／无效Key／两次本机模拟请求。默认新HOME没有已就绪的登录钥匙串，本机观察为 `credential_locked`；不得为测试自动建立或解锁系统库。可单独显式指定 `--use-login-keychain-context` 验证当前登录用户的系统环境；产品的库与凭据目录仍固定在新stage，报告准确标明非隔离HOME。当前原生配置／新进程执行有实际通过证据，但开发解释器跨程序自动清理未通过；脚本整体保留失败，不把受控维护清理变成自动降级。只有成功操作返回、资料提交与元数据均匹配的合成项进入精确清理清单，未知结局不猜测清理。

## 缺项通过同一初始化向导安装的方案

1. 显示各能力当前是否可用、预计下载体积、供应来源、许可及保存目录；模型/API Key 不作为软件安装的前置输入。
2. 用户分别确认浏览器、媒体工具、OCR；下载与本机文件安装不授予平台同步、模型上传或自动提取权限。
3. 浏览器固定 Playwright 版本匹配的 Chromium 官方运行包。FFmpeg 各系统从核验的官方认可发行源选择并审查 LGPL/GPL 构建选项；无法确认许可时不自动分发。OCR 固定运行库与权重版本/许可。
4. 每份下载固定 HTTPS 来源、大小、SHA-256 和目标平台，先写专用临时目录，验证后原子安装；拒绝归档越界、未知链接与冲突。唯一已核实的浏览器归档可保留其五个编译固定的内部相对链接，不接受用户提供的链接策略。中断保留可诊断状态，不隐式重试云请求。
5. 安装后实际运行探测，再显示就绪；失败保持已建资料库，不启用定时同步或任何费用。用户可跳过并明确获得仅读库/管理页的降级状态。

2026-10-02 已实现共享固定清单安装器与本地CLI：明确确认、内核独占安装锁、完整包大小／hash、受控解包、分代保留及原子收据。固定HTTPS下载不继承代理、不携带凭据、不自动重试。已核实清单包含Mac14+ ARM64无桌面浏览器与可见浏览器开发候选；FFmpeg配对、OCR权重与其他平台尚未提供。资料库可不存在，安装不会创建库或启动处理任务。浏览器可见窗口本机渲染已经源码及独立程序验证，但真实抖音登录未验收，公开发行许可、签名与公证未关闭。

桌面候选新增“安装运行组件”入口：先异步只读检查，然后打开固定清单，显示主机状态、下载体积、来源、许可及缺项。没有默认选中项；用户选择单项后还须再次确认。无桌面浏览器不能代替可见登录浏览器；可见候选单独选择，不支持的主机不能点击安装。安装只写产品固定的库外运行目录，页面不能传入URL、安装路径或命令；模型配置、登录、模型执行与来源执行四个授权保持原值。

安装与服务启动互斥；安装期间不能切换资料库或启动服务。选择、停止、退出或参数变化会废弃旧确认意图。停止只请求协作取消，退出保持窗口直到真实安装线程结束；网络等待不冒充已经中断。静态安装成功不显示为登录、同步或识别已通过，实际功能仍须独立探测。此入口的离线回归、冻结程序及实际窗口证据分别记录，不把替身测试当三端或新用户验收。

以绝对路径替换下面的三个独立目录；不要指向旧库、旧账号或其他项目运行目录。原生控制台加前缀 `CollectionContext cli`，源码安装后使用 `collection-context`：

```bash
collection-context --workspace /absolute/new-library runtime-options
collection-context --workspace /absolute/new-library --runtime-dir /absolute/product-runtime install-runtime --artifact chromium-headless-macos-arm64-1243 --confirm-install
collection-context --workspace /absolute/new-library --runtime-dir /absolute/product-runtime probe-runtime --browser-dir /absolute/new-probe-profile

# 可见窗口候选，单独安装与单独新profile探测；不启动平台登录。
collection-context --workspace /absolute/new-library --runtime-dir /absolute/product-runtime install-runtime --artifact chromium-macos-arm64-1243 --confirm-install
collection-context --workspace /absolute/new-library --runtime-dir /absolute/product-runtime probe-runtime --browser-dir /absolute/new-visible-probe-profile --headed
```

查看命令只显示大小／系统／来源／许可与缺项；确认安装仅授权这份软件，不授权登录、同步或模型费用。安装成功初始为static_verified，功能仍false；探测显式启动新空白浏览器，本机页面成功且关闭后才报告功能通过。探测目录必须不存在，拒绝借用用户登录态；该结果不等于五类抖音来源可用。固定软件下载包最多2GiB：不超过128MB沿用缓冲传输／120秒，超过则专用64KiB分块写盘／300秒；普通媒体下载的128MB上限不变。取消在阻塞网络或文件操作后观察，不谎报立即退出；失败暂存、旧代保留。receipt发布异常可返回outcome_unknown，不自动回滚或重试。

`runtime_smoke.py --binary <明确控制台> --runtime <已验证安装目录> --output <不存在的新库外目录>` 已以新HOME／空PATH验证原生包内驱动、浏览器本机渲染／退出及收据不变；不下载、不接平台、不读模型Key。精确候选／报告见实施记录。普通CLI／MCP／HTTP／停止及异路径运行仍单独验收，构建成功不能代替运行证明。原生许可、签名、全部组件的桌面安装与三端发行仍未完成。

可见候选使用上述脚本的显式 `--headed`，报告为 `frozen_headed_local_fixture_only`，不能用headless结果充当该项通过。Chrome for Testing归档固定完整hash，五个framework相对链接名称／目标均须精确匹配并留在普通节点构成的本代树内；泛用资料读取、其他归档及可执行文件路径仍拒绝链接。保留ABOUT、Widevine许可和内置credits／terms，不修改签名、不移除quarantine、不绕过系统安全保护。每次使用重验主执行文件与已知五个链接，但不声称完整资源树每次重新hash。
