# R2 Manager

一个面向 Windows 的 Cloudflare R2 图形化对象管理工具。无需记忆 S3 命令，即可在桌面端浏览 Bucket 中的对象、上传和下载文件、创建虚拟文件夹，以及删除对象或整个前缀目录。

> 本项目通过 Cloudflare R2 的 S3 兼容 API 访问存储；它不是 Cloudflare 官方客户端。请在使用前确认自己拥有目标 Bucket 的授权。

## 功能

- 浏览 Bucket 中的对象，并按“文件夹”层级查看
- 上传多个文件到当前目录
- 下载一个或多个选中的对象
- 新建虚拟文件夹（创建以 `/` 结尾的零字节对象）
- 删除文件；删除文件夹时递归删除该前缀下的所有对象
- 显示文件大小、类型和修改时间，支持排序、复制完整对象 Key 和右键操作
- 支持中文等非 ASCII 文件名
- 保存最近成功访问的 Bucket 名称

## 系统要求

- Windows 10 或 Windows 11
- Python 3.10 或更高版本，并已加入 `PATH`
- 可访问 Cloudflare R2 的网络连接
- 已启用 R2 的 Cloudflare 账号、一个 Bucket，以及该 Bucket 的 R2 API 凭证

应用界面使用 Python 自带的 Tkinter；不需要单独安装 GUI 框架。

## 快速开始

### 1. 克隆项目

```powershell
git clone https://gitee.com/yoursmengle/r2client.git
Set-Location r2client
```

### 2. 启动应用

```powershell
.\start.ps1
```

`start.ps1` 会自动完成以下工作：

1. 检查 `uv`；如果未安装，会通过 `python -m pip` 安装。
2. 在项目目录创建 `.venv` 虚拟环境（首次运行）。
3. 按 `requirements.txt` 安装依赖。
4. 启动 `r2_manager.py`。

如果 PowerShell 阻止脚本执行，请只为本次启动绕过执行策略：

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\start.ps1
```

不要为了运行本项目而永久放宽系统的 PowerShell 执行策略。

### 手动启动（可选）

如果不使用启动脚本，也可手动创建环境并运行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe .\r2_manager.py
```

## 申请并配置 Cloudflare R2

本节说明从零开始准备本工具所需的 R2 资源。Cloudflare 的界面文字可能随版本略有调整，应以其[官方 R2 入门文档](https://developers.cloudflare.com/r2/get-started/)为准。

### 1. 开通 R2

1. 注册或登录 [Cloudflare Dashboard](https://dash.cloudflare.com/)。
2. 进入 **Storage & databases → R2 → Overview**。
3. 按页面提示完成 R2 订阅/结账流程。

R2 提供包含免费额度的入门用量，但仍需完成订阅流程，超出免费额度会按实际使用量计费。费用和免费额度可能调整，请查看 [Cloudflare R2 Pricing](https://www.cloudflare.com/developer-platform/r2/pricing/) 的最新说明。

### 2. 创建 Bucket

在 **R2 → Overview** 中选择 **Create bucket**，然后：

1. 输入 Bucket 名称，例如 `my-r2-files`。
2. 选择数据位置和默认存储类型；若不确定，通常可先使用默认位置与 `Standard`。
3. 确认创建。

Bucket 名称只能由小写字母、数字和连字符组成，长度为 3–63 个字符，且不能以连字符开头或结尾。详见 [Cloudflare 创建 Bucket 文档](https://developers.cloudflare.com/r2/buckets/create-buckets/)。

### 3. 创建最小权限的 R2 API Token

本工具需要列举、读取、上传和删除对象。因此请为**目标 Bucket**创建一个单独的、范围受限的对象读写凭证：

1. 在 **R2 → Overview** 的 **API Tokens** 区域选择 **Manage**。
2. 选择 **Create Account API token** 或 **Create User API token**。
3. 在权限中选择 **Object Read & Write**。
4. 将适用范围限制为 **Apply to specific buckets only**，并选择刚创建的 Bucket。
5. 创建令牌后，立即安全地保存以下两个值：
   - **Access Key ID**
   - **Secret Access Key**

`Secret Access Key` 只会显示一次；丢失后需要新建凭证并撤销旧凭证。本应用不需要 Cloudflare 的通用 API Token，也不需要 Bucket 管理员权限。令牌类型与权限说明请参阅 [Cloudflare R2 Authentication](https://developers.cloudflare.com/r2/api/tokens/)。

### 4. 获取 S3 API Endpoint

在创建令牌后的确认页，或 R2 Overview 页面中，复制 S3 API Endpoint。普通 Bucket 的格式为：

```text
https://<ACCOUNT_ID>.r2.cloudflarestorage.com
```

将 `<ACCOUNT_ID>` 替换为 Cloudflare Account ID；不要把 Bucket 名称写入 Endpoint。使用 EU 或 FedRAMP jurisdiction 创建的 Bucket 必须使用其对应的 jurisdiction 专用 Endpoint。具体格式见 [Cloudflare S3 API 文档](https://developers.cloudflare.com/r2/api/s3/api/)。

### 5. 在 R2 Manager 中连接

首次启动时会出现凭证设置窗口。按下表填入：

| 应用字段 | 填写内容 |
| --- | --- |
| Access Key ID | Cloudflare 生成的 **Access Key ID** |
| Secret Access Key | Cloudflare 生成的 **Secret Access Key** |
| Endpoint URL | `https://<ACCOUNT_ID>.r2.cloudflarestorage.com` |

保存后，在窗口右上角的 **Bucket** 输入框中填入 Bucket 名称并按 Enter，或点击刷新。若对象列表正常加载，说明连接成功。

## 使用说明

### 浏览与切换目录

- 在右上角输入 Bucket 名称后按 Enter 加载对象。
- 左侧目录树用于切换“文件夹”；右侧列表显示当前前缀下的项目。
- 双击文件夹可进入，按 Backspace 或使用 **Go Up** 返回上级目录。
- 点击表头可按名称、大小、类型或修改时间排序。

R2 是对象存储，不存在真正的目录。本应用把对象 Key 中以 `/` 分隔的前缀显示为文件夹；因此目录操作实际作用于对象前缀。

### 上传文件

1. 进入目标目录。
2. 点击工具栏的上传按钮，或选择 **File → Upload File(s)…**。
3. 选择一个或多个本地文件。

文件会被上传到当前目录，并保留本地文件名。上传时会显示进度和成功/失败数量。

### 下载文件

1. 在右侧列表中选择一个或多个文件。
2. 点击下载按钮、右键选择 **Download**，或使用 **File → Download Selected…**。
3. 选择本地下载目录。

下载会将文件直接保存到所选目录。若选择了不同 R2 路径下的同名文件，后下载的文件可能覆盖先下载的文件；请分批下载或改用不同的目标目录。

### 新建文件夹

在目标目录点击 **New Folder**，输入名称并确认。应用会创建一个以 `/` 结尾的零字节占位对象，使其在对象浏览器中显示为文件夹。

### 删除文件或文件夹

选择一个或多个项目后点击删除，或按 Delete 键。删除前会显示确认提示。

> **警告：删除不可撤销。** 删除文件夹会删除该前缀下的全部对象（含子目录），而不只是空目录。请在确认对话框中核对对象数量和名称。

## 凭证与安全

应用使用 AWS Signature Version 4 与 R2 S3 API 通信，并使用以下名称保存已填写的凭证：

| 名称 | 用途 |
| --- | --- |
| `R2_ACCESS_KEY` | R2 Access Key ID |
| `R2_SECRET_KEY` | R2 Secret Access Key |
| `R2_ENDPOINT` | R2 S3 API Endpoint |

在 Windows 上，这些值会写入当前用户的环境变量注册表项（`HKCU\Environment`），并在后续启动时自动读取。它们不会写入项目的普通配置文件或提交到 Git，但也**不是加密存储**。请不要在共享 Windows 账户、录屏、截图或日志中泄露 Secret Access Key。

最近使用的 Bucket 名称会保存在应用目录的 `.r2_bucket` 文件中；该文件不包含密钥，且已被 Git 忽略。

### 轮换或移除凭证

- **轮换：** 在 Cloudflare 创建新 token，在应用的 **Settings → API Credentials…** 中替换三项配置，确认可用后在 Cloudflare 撤销旧 token。
- **移除本机凭证：** 关闭应用后，在 PowerShell 中执行：

  ```powershell
  [Environment]::SetEnvironmentVariable('R2_ACCESS_KEY', $null, 'User')
  [Environment]::SetEnvironmentVariable('R2_SECRET_KEY', $null, 'User')
  [Environment]::SetEnvironmentVariable('R2_ENDPOINT', $null, 'User')
  ```

  然后重新打开应用。请同时在 Cloudflare Dashboard 撤销不再使用的 token。

## 构建独立 Windows 可执行文件

项目提供 PyInstaller 构建脚本：

```powershell
.\build.ps1
```

脚本会准备构建依赖并生成单文件、无控制台窗口的应用：

```text
dist\R2Manager.exe
```

构建过程中会重建 `build/` 和 `dist/` 产物目录；这些文件均已被 Git 忽略。请在干净的 Windows 测试环境中验证生成的 `.exe` 后再发布。

## 项目结构

```text
.
├── r2_manager.py   # Tkinter 界面、R2 请求与 AWS SigV4 签名
├── start.ps1       # 自动准备环境并启动应用
├── build.ps1       # 使用 PyInstaller 打包 Windows .exe
├── requirements.txt # Python 运行依赖
└── .gitignore      # 忽略本地 Bucket 记录与构建产物
```

## 依赖

- [requests](https://pypi.org/project/requests/)：发送 R2 的 HTTP 请求
- [r2client](https://pypi.org/project/r2client/)：提供 MIME 类型辅助能力

## 已知限制

- 当前正式支持 Windows；凭证持久化依赖 Windows 注册表。
- 上传实现会先将整个文件读入内存，再发送到 R2；不适合超大文件或需要分块/断点续传的场景。
- 该工具不提供 Bucket 创建、列出所有 Bucket、公开域名、CORS、生命周期规则或访问策略管理；请在 Cloudflare Dashboard 中完成这些管理操作。
- 项目目前没有自动化测试或 CI。发布前建议至少手动验证连接、上传、下载和删除流程。

## 贡献

欢迎提交 Issue 和 Pull Request。提交前请在 Windows 上完成与修改相关的手动验证，并确保不提交 `.r2_bucket`、Access Key、Secret Access Key、构建产物或其他敏感信息。

## 许可证

本项目基于 [GNU Affero General Public License v3.0 (AGPL-3.0)](LICENSE) 发布。使用、修改和分发本项目时，请遵守该许可证的完整条款。
