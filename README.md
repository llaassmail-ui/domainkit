# domainkit

域名上线自动化工具，版本 2.0.0。

## Spaceship DNS

DNS 服务商支持 GoDaddy 和 Spaceship，未配置时使用 GoDaddy。先自行购买域名，并确保 DNS zone 由所选服务商托管。

在 Spaceship 账户的 API 管理页面创建 API Key，保存 Key 和 Secret；推荐仅授予 `dnsrecords:read`、`dnsrecords:write`，`domains:read` 可选。不需要 `domains:billing`、`domains:write`、`domains:transfer`、`contacts:write`。

GUI 单域名页面选择 `spaceship`，在凭证页面填写 API Key、API Secret 和 Endpoint，点击保存凭证。勾选记住凭证时沿用 Windows DPAPI 本机加密存储；输入框默认隐藏密钥。测试连接前填写要检查的根域名。

Endpoint 默认为官方文档地址 `https://spaceship.dev/api`，允许改为其他 HTTPS 地址。CLI 环境变量为 `SPACESHIP_API_KEY`、`SPACESHIP_API_SECRET`、`SPACESHIP_API_ENDPOINT`；非秘密配置字段为 `spaceship_endpoint`。

```console
domainkit doctor --dns-provider spaceship --domain example.com
domainkit plan example.com --dns-provider spaceship --create-distribution --origin example.s3.amazonaws.com --origin-type custom --origin-path /website/example.com
```

自检只验证读取权限，不写 DNS 来探测写权限。401 表示凭证无效；403 表示权限不足；404 需检查 Endpoint、路径及域名；网络错误需检查连接。

Spaceship 写入前读取现有记录并检查冲突，使用 `force: false` 只提交需要新增的记录。官方 PUT 接口采用添加记录或更新 TTL 的语义，无关记录不会重新提交，正确记录跳过，不同目标或 CNAME 类型冲突时报错。精确删除仅支持 DNS 记录，要求名称、类型、目标值匹配。

裸域名输入时只申请并绑定 `www.<domain>`，不申请 `*.www.<domain>`。Spaceship 的裸域 301 需要在控制台手工配置 `example.com -> https://www.example.com`，工具会明确提示，不创建额外跳转分配。

批量 JSON 的 `defaults.dns_provider` 是默认值，单项 `dns_provider` 优先。参见 `batch.example.json` 中的混用示例；源站支持 `{"domain":"example.s3.amazonaws.com","type":"custom","path":"website/example.com"}`。

范围仅限 DNS、ACM、CloudFront 和 GitLab，不包含域名购买、续费、删除、转移、支付或联系人管理。凭证及本机配置由 `.gitignore` 排除，禁止将真实密钥写入示例或代码。API 依据：[Spaceship 官方文档](https://docs.spaceship.dev/)。

在 GoDaddy 买好域名之后，把域名和源站信息交给它，剩下的事情它一次跑完：申请 ACM 证书、写 GoDaddy 验证记录、等证书签发、创建 CloudFront 分配并绑定 `www.<域名>` 备用域名和证书、把 `www` 解析到分配域名、提示/尝试把主域名 301 转发到 `https://www.`、在 GitLab 从 `dev` 拉出以域名命名的分支、写好 `CLOUDFRONT_<域名大写>` 这个 CI/CD 变量。

每一步都是幂等的。中断之后重跑会认领上一次已经建好的资源，不会重复创建第二份。

## 它替你做的事

| 步骤名 | 做什么 |
| --- | --- |
| `certificate` | 在 us-east-1 申请证书。裸域名输入时默认只申请 `www.<域名>`；已有可复用证书则直接复用 |
| `dns-validation` | 把 ACM 给的 CNAME 验证记录写进 GoDaddy |
| `certificate-issued` | 可选，阻塞等待证书变成 ISSUED，默认最多 600 秒（10 分钟） |
| `cloudfront-create` | 找不到现成分配时，按你给的源站新建一个 |
| `cloudfront` | 给分配挂上 `www.<域名>` 备用域名和证书 |
| `site-dns` | 写 `www` 的 CNAME，指向分配域名 |
| `apex-forward` | 尝试用 GoDaddy 域名转发把主域名 301 到 `https://www.<域名>`；如果 GoDaddy API 不支持，会给出手动操作提示且不中断流程 |
| `gitlab` | 从 `dev` 建分支，分支名就是域名；写 CI/CD 变量 |

## 安装

只用 Python 标准库，不需要装任何第三方包，Python 3.9 以上即可。

```bash
cd domainkit
pip install -e .
```

不想装也行，在项目目录里直接跑：

```bash
python -m domainkit --help
python -m domainkit.gui
```

## 凭证配置

凭证可以通过两种方式配置：

### 方式一：GUI 界面配置（推荐）

启动 GUI 后，点击左侧「凭证与项目」，在界面中填写凭证信息。

1. 填写 AWS、GoDaddy、GitLab 的凭证
2. GitLab 只需要先填 `GitLab URL` 和 `Access Token`，然后点击「加载 GitLab 项目」
3. 在「项目列表」里选择目标仓库，工具会把稳定的数字 Project ID 自动填入 `Project ID`，同时同步到单域名页的「GitLab 项目」
4. 勾选「记住凭证(本机加密)」复选框（推荐）
5. 点击「测试连接」验证配置是否正确

**特点：**
- 敏感信息默认显示为 `****`，可点击「显示」复选框查看
- 勾选「记住凭证」后，使用 **Windows DPAPI** 加密保存到本机 `%LOCALAPPDATA%\domainkit\credentials.json`，只有当前 Windows 用户能解密
- 下次启动 GUI 自动加载，不需要重新输入
- 保存后日志会显示掩码摘要（例如 `glpa******** (长度 20)`），确认哪些字段已生效
- 空白字段不会写入，也不会覆盖已保存的值
- 「加载 GitLab 项目」只读取当前 Token 可访问的项目列表，不会修改 AWS、GoDaddy 或 GitLab 项目
- 点击「查看已保存」可查看当前本机存储的掩码摘要
- 点击「清空所有」会同时清空输入框和本机存储

**安全说明：**
- Windows DPAPI 绑定当前用户 + 当前机器，换机器或换用户无法解密
- 明文密钥只在内存中短暂停留，文件里全是密文
- 如果不勾选「记住凭证」，关闭程序后凭证只在当前会话生效，下次需要重新输入

### 方式二：环境变量配置

三组凭证也可以通过环境变量设置。CLI 和 GUI 都会读取环境变量，但 GUI 保存的凭证优先级更高。

```bash
# AWS，ACM 和 CloudFront 共用
export AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE
export AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY
export AWS_SESSION_TOKEN=          # 只有临时凭证需要

# GoDaddy
export GODADDY_API_KEY=your_key
export GODADDY_API_SECRET=your_secret

# GitLab
export GITLAB_URL=https://gitlab.com
export GITLAB_PROJECT_ID=group/repo
export GITLAB_TOKEN=glpat-xxxxxxxxxxxxxxxxxxxx
```

Windows PowerShell 用 `$env:AWS_ACCESS_KEY_ID='...'` 这种写法。

配置齐了先验一遍连通性：

```bash
domainkit doctor
```

它会分别探一次 AWS、GoDaddy、GitLab，哪一项不通就单独报哪一项，全通返回 0。

## 怎么创建 GitLab 访问令牌

工具要做两件需要写权限的事：从 `dev` 创建分支，以及创建或更新项目的 CI/CD 变量。两种令牌都可以，推荐第一种。

**权限说明：** 工具只需要 `api` scope，不需要 `write_repository`、`read_repository` 等其他权限。角色至少 `Maintainer`，因为写 CI/CD 变量需要 Maintainer 权限（Developer 能建分支但改不了变量会 403）。

### 方式一：项目访问令牌（推荐）

权限范围只限这一个项目，泄漏影响面小。需要项目的 Maintainer 或 Owner 角色才能创建。

1. 打开项目页面，左侧菜单 `Settings` → `Access tokens`
2. `Token name` 随便填，比如 `domainkit`
3. `Expiration date` 按你的安全策略设，GitLab 现在强制要有过期时间
4. `Select a role` 选 `Maintainer`，Developer 角色改不了 CI/CD 变量
5. `Select scopes` 勾 `api`
6. 点 `Create project access token`

令牌只显示一次，离开页面就再也看不到，当场复制走。

### 方式二：个人访问令牌

一个令牌要管多个仓库时用这个，比如批量处理里 `gitlab_project` 指向好几个项目。

1. 右上角头像 → `Edit profile` → 左侧 `Access tokens`
2. 填名字和过期时间
3. `Select scopes` 勾 `api`
4. 点 `Create personal access token`

个人令牌继承你本人在各项目里的角色，所以你在目标项目里至少得是 Maintainer。

### 自建 GitLab

把 `GITLAB_URL` 改成你自己的地址，例如 `export GITLAB_URL=https://gitlab.mycorp.com`，其余步骤一样。

### 项目 ID 怎么填

`GITLAB_PROJECT_ID` 支持两种写法，数字 ID 如 `12345678`，或者带命名空间的路径如 `mygroup/frontend`。路径写法里工具会自动做 URL 编码，你不用自己转义斜杠。

GUI 里推荐不用手填：填好 `GitLab URL` 和 `Access Token` 后，点「加载 GitLab 项目」。工具会调用 GitLab API 读取这个 Token 能看到的项目，支持用「项目搜索」缩小范围。选中项目后，`Project ID` 会自动回填为数字 ID；保存凭证后，下次启动会继续使用这个数字 ID。

注意：Token 不能直接“反推出唯一项目”。如果是 Project Access Token，通常只能列出所属项目；如果是个人令牌，可能会列出很多项目，所以工具必须让你手动选一次，避免猜错仓库。

## AWS IAM 权限

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "acm:RequestCertificate",
        "acm:DescribeCertificate",
        "acm:ListCertificates",
        "acm:AddTagsToCertificate"
      ],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "cloudfront:CreateDistribution",
        "cloudfront:CreateDistributionWithTags",
        "cloudfront:GetDistribution",
        "cloudfront:GetDistributionConfig",
        "cloudfront:UpdateDistribution",
        "cloudfront:ListDistributions",
        "cloudfront:TagResource"
      ],
      "Resource": "*"
    }
  ]
}
```

证书必须在 us-east-1，这是 CloudFront 的硬性要求，工具已经把 ACM 客户端固定在这个区域。

## GUI

```bash
python -m domainkit.gui
```

在当前项目目录也可以直接运行 `./domainkit.cmd gui`。GUI 依赖 Python 自带的 Tcl/Tk 组件；如果启动时报 `缺少 Tcl/Tk 运行文件` 或 `init.tcl`，请安装官方 Python 3.11/3.12，并在安装时保留 `tcl/tk and IDLE` 组件，或者把 `domainkit.cmd` 里的 `PY` 改成一套完整 Python 的路径。

窗口现在按运维工作台组织：顶部是 AWS、GoDaddy、GitLab、当前项目和运行状态；左侧是导航；中间是当前页面；底部保留完整实时日志。

1. **单域名上线**：常用字段分成「基础信息」「源站与 CloudFront」「执行选项」。输入域名后会实时显示 `www.<domain>`、GitLab 分支、CI/CD 变量名和证书域名；填完先点「预演」，确认没问题再点「执行」。执行前会列出将真实变更的服务。
2. **批量上线**：选一个 JSON 文件，可以先「加载批次」看看解析出多少条，然后「批量预演」和「批量执行」。这一页保留原 JSON 格式，不强制通过 GUI 编辑批次文件。
3. **凭证与项目**：填写 AWS、GoDaddy、GitLab 凭证；GitLab 可以通过「加载 GitLab 项目」从 Token 可访问项目里选择目标仓库，选中后自动回填数字 Project ID。勾选「记住凭证」后下次启动自动加载。详见「凭证配置」章节。
4. **日志**：底部日志会一直显示，这一页提供清空、复制、保存日志的快捷操作。

单域名页有「结果摘要」表格，预演或执行后会按步骤显示 ACM 证书、DNS 验证、CloudFront、站点 DNS、裸域名 301、GitLab 分支和变量的状态。底部日志仍保留完整原文，内容跟 CLI 一致。

单域名页可以点击「保存默认配置」，把 GitLab 项目、基准分支、当前源域名、源类型、源路径和常用开关保存到 `%LOCALAPPDATA%\\domainkit\\gui-defaults.json`；这里不保存域名和 Distribution ID，避免误操作复用。

源站可以保存多条预设。填好「源域名」「源类型」「源路径」后点「保存当前源站」，下次启动会出现在「源站预设」下拉框；选中一条会自动回填这三项。「预设搜索」可以按源域名或路径关键字筛选，比如输入 `ruby-rummy` 再点「筛选」。源站预设同样保存在 `%LOCALAPPDATA%\\domainkit\\gui-defaults.json`，不是密钥，不走 DPAPI 加密。

等待证书的超时在 GUI 里固定为默认的 600 秒（10 分钟），需要更长时间请用命令行的 `--wait-timeout`。超时显示“等待验证，可稍后重试”，暂停后续步骤；重新执行会复用已有证书。

执行前会弹一次确认框。真正的调用跑在后台线程里，界面不会卡住。

## 命令行

### 看看会生成什么名字

不发任何请求，纯本地推导，适合先确认命名对不对。

```bash
domainkit names livingtechnology.com
```

```
livingtechnology.com
  service host  www.livingtechnology.com
  branch        livingtechnology.com
  variable key  CLOUDFRONT_LIVINGTECHNOLOGY_COM
  dns zone      livingtechnology.com
  aliases       www.livingtechnology.com
  certificate   www.livingtechnology.com
```

### 预演

```bash
domainkit plan livingtechnology.com \
  --create-distribution \
  --origin livingtechnology.s3-website-us-east-1.amazonaws.com
```

`plan` 只读，不会改任何东西。输出里每行前面的符号表示动作：`+` 新建，`~` 修改，`=` 已符合预期跳过，`?` 待执行的计划，`-` 主动跳过，`!` 失败。

### 执行

```bash
domainkit apply --yes livingtechnology.com \
  --create-distribution \
  --origin livingtechnology.s3-website-us-east-1.amazonaws.com \
  --wait
```

`--yes` 是必须的，缺了它直接返回退出码 2 并提示你补上，避免手滑。

`--wait` 会在写完验证记录后等待 ACM 签发，默认最多等 600 秒，用 `--wait-timeout 900` 可以调长。等待超时后停止后续操作，稍后重新执行即可复用证书继续。批量和 CLI 保留非成功状态，避免把未完成任务当作完成。

### 常用参数

| 参数 | 说明 |
| --- | --- |
| `--create-distribution` | 找不到现成分配时允许新建，必须同时给 `--origin` |
| `--origin` | 源站域名，如 `site.s3-website-us-east-1.amazonaws.com` |
| `--origin-type` | `s3-website` 或 `custom`，默认前者 |
| `--origin-path` | 源站路径前缀，要以 `/` 开头 |
| `--spa` | 把 403 和 404 都映射到 `/index.html`，单页应用需要 |
| `--distribution-id` | 直接指定分配，跳过自动发现 |
| `--alias` | 额外别名，可以重复给 |
| `--project` | 覆盖 `GITLAB_PROJECT_ID` |
| `--base-branch` | 覆盖基准分支，默认 `dev` |
| `--wait` / `--wait-timeout` | 等证书签发，仅 `apply` 有 |
| `--skip-dns` | 完全不碰 GoDaddy |
| `--skip-site-dns` | 仍写证书验证记录，但不写 `www` 也不设转发 |
| `--skip-gitlab` | 不碰 GitLab |
| `--json` | 全局参数，放在子命令前面，输出机器可读的 JSON |

### 查看已有变量

```bash
domainkit variables --prefix CLOUDFRONT
```

## 批量处理

批次文件是一个 JSON 对象，`defaults` 是公共配置，`domains` 里每一项都继承 `defaults` 并可以覆盖其中任意字段。想让一批域名进同一个仓库，就把 `gitlab_project` 放在 `defaults` 里；想让不同域名进不同仓库，就在对应条目里单独写 `gitlab_project`。

```json
{
  "defaults": {
    "gitlab_project": "group/frontend",
    "base_branch": "dev",
    "origin_type": "s3-website",
    "create_distribution": true,
    "wait_certificate": false
  },
  "domains": [
    {
      "domain": "example.com",
      "origin": "example.s3-website-us-east-1.amazonaws.com"
    },
    {
      "domain": "shop.example.com",
      "origin": "shop.s3-website-us-east-1.amazonaws.com",
      "gitlab_project": "group/shop",
      "spa": true
    },
    {
      "domain": "legacy.com",
      "distribution_id": "E1234567890ABC"
    }
  ]
}
```

```bash
domainkit batch batch.json                # 预演
domainkit batch batch.json --apply --yes  # 执行
```

可用字段：`domain`、`origin`、`origin_type`、`origin_path`、`distribution_id`、`aliases`、`spa`、`default_root_object`、`price_class`、`create_distribution`、`wait_certificate`、`certificate_timeout`、`skip_dns`、`skip_site_dns`、`skip_gitlab`，以及 `gitlab_url`、`gitlab_project`、`base_branch`、`variable_prefix`、`dns_ttl` 这几个配置项。没在条目里出现的配置项沿用环境变量和配置文件里的值。

失败处理分两层。文件本身有问题会直接中止，比如不是 JSON 对象、`domains` 不是数组、一个域名都没有。单个条目的问题只记进 `failures` 然后继续跑下一个，比如缺 `domain`、要求创建却没给 `origin`、或者跑到一半 API 报错。最后汇总成功和失败的数量，只要有失败就返回退出码 1。

参考文件在 `batch.example.json`。

## 配置文件

除了凭证，其余默认值可以放在配置文件里，省得每次敲参数。当前目录的 `domainkit.json` 会被自动读取，也可以用 `--profile path/to/file.json` 指定。

```json
{
  "gitlab_url": "https://gitlab.com",
  "gitlab_project": "your-group/your-deploy-repo",
  "base_branch": "dev",
  "variable_prefix": "CLOUDFRONT",
  "variable_protected": false,
  "variable_masked": false,
  "dns_ttl": 600,
  "minimum_tls": "TLSv1.2_2021",
  "tags": {
    "managed-by": "domainkit"
  }
}
```

优先级是命令行参数 > 环境变量 > 配置文件 > 内置默认值。`dns_ttl` 不能低于 600，这是 GoDaddy 的下限。参考文件在 `domainkit.example.json`。

## 命名规则

域名决定其余所有名字，没有额外的映射表要维护。

`livingtechnology.com` 会得到分支 `livingtechnology.com`、变量 `CLOUDFRONT_LIVINGTECHNOLOGY_COM`、证书 `livingtechnology.com` 加 `*.livingtechnology.com`。

`shop.example.com` 会得到分支 `shop.example.com`、变量 `CLOUDFRONT_SHOP_EXAMPLE_COM`。变量名是把域名所有点换成下划线再全部大写，所以三级域名会得到三段。DNS 操作落在可注册域 `example.com` 上。

你最初给的例子写的是 `CLOUDFRONT_LIVINGTECHNOLGY_COM`，`TECHNOLGY` 少一个 `O`。工具是机械地从域名推导变量名的，所以 `livingtechnology.com` 一定生成 `CLOUDFRONT_LIVINGTECHNOLOGY_COM`。如果你的域名本来就注册成了 `livingtechnolgy.com`，那生成的变量名自然也跟着少一个 `O`，两边始终一致。

## 几个需要知道的细节

### 主域名为什么用转发而不是解析

GoDaddy 的 DNS 不支持在根域名上放 CNAME 或 ALIAS，所以无法把 `example.com` 直接指到 CloudFront。工具的做法是把 CloudFront 备用域名和证书都配置到 `www.example.com`，再把 `www.example.com` 用 CNAME 指向分配域名。主域名 `example.com` 只能做 301 跳转到 `https://www.example.com`，不开 URL 遮蔽。实测 GoDaddy 公开 API 的 `/v1/domains/forwards/{domain}` 在部分账号/域名上返回 `404 There is no method to handle request`，所以工具会尝试自动设置；如果 API 不支持，会把 `apex-forward` 标记为 skipped 并提示你去 GoDaddy 控制台手动设置，流程不会因此失败。

如果你更希望根域名直接指向 CloudFront，得把 DNS 托管迁到 Route 53 用 ALIAS A 记录，那超出了这个工具的范围。

### ACM 验证记录名是相对的

ACM 返回的验证记录名是完全限定域名，比如 `_9c8f1a.livingtechnology.com`，但 GoDaddy 的 DNS 接口要的是相对于域的名字，也就是 `_9c8f1a`。工具会自动去掉后面的域名部分再提交，根域名本身会写成 `@`。你在 GoDaddy 控制台看到的记录名比 ACM 页面上短一截，这是正常的。

### 源站按公开可读的桶来配

默认假设你的 S3 桶是公开可读的静态网站托管，所以两种源站类型都走 CloudFront 的自定义源配置，不创建 OAC 或 OAI。`s3-website` 类型回源用 HTTP，因为 S3 网站端点不支持 HTTPS；`custom` 类型回源强制 HTTPS。访问者一侧两种都是 `redirect-to-https`。

计费模式按 Pay as you go 处理，不发送 WAF 相关字段。

### 分配是怎么找到的

按四个信号依次尝试：命令行给的 `--distribution-id`；用域名去比对现有分配的别名；用分配备注里的 `[domainkit:<域名>]` 标记找上一次没跑完的分配；最后读 GitLab 里已有的那个 CI/CD 变量值。都找不到，且你给了 `--create-distribution` 和 `--origin`，就新建一个。GitLab 里的变量值如果已经失效，工具会记一笔然后继续，不会因此中止。

### 证书等待超时怎么办

默认等待 10 分钟，以留出 DNS 生效和 ACM 验证的时间。超时不代表证书申请失败，AWS 仍会继续验证；稍后重跑即可，无需删除或重新申请证书。长时间未签发时，请核对权威 DNS 和验证 CNAME。

### 尚未在真实环境验证过

全部核心逻辑由 152 个单元测试覆盖，HTTP 层用假传输层拦下所有请求，SigV4 签名对过 AWS 官方测试向量。工具已经在你的真实 AWS、GoDaddy、GitLab 环境里跑通过域名上线流程；但 GoDaddy 主域名转发 API 在当前账号/域名上返回过 404，所以 `apex-forward` 可能仍需要去 GoDaddy 控制台手动设置。第一次给新仓库或新配置使用时，仍建议先跑 `plan` 或 GUI「预演」，确认资源复用和创建计划没问题再执行。

### GoDaddy 生产 API 的门槛

GoDaddy 的生产环境 API key 要求账号下有一定数量的活跃域名，新账号可能只能用测试环境。如果 `doctor` 这一项过不去，可以先加 `--skip-dns` 把 AWS 和 GitLab 的部分跑完，DNS 手动配。

## 开发

```bash
python -m unittest discover -s tests -t .
```

```
domainkit/
├── domainkit/
│   ├── aws.py          # ACM 与 CloudFront 客户端
│   ├── batch.py        # 批次文件加载与执行
│   ├── cli.py          # 命令行入口
│   ├── config.py       # 配置加载
│   ├── errors.py       # 异常定义
│   ├── gitlab.py       # GitLab 客户端
│   ├── godaddy.py      # GoDaddy DNS 与域名转发客户端
│   ├── gui.py          # Tkinter 界面
│   ├── http.py         # HTTP 传输层抽象
│   ├── naming.py       # 域名解析与命名推导
│   ├── sigv4.py        # AWS SigV4 签名
│   └── workflow.py     # 主流程编排
├── tests/
├── batch.example.json
├── domainkit.example.json
├── SPEC.md
└── pyproject.toml
```

`http.py` 把所有网络调用收在一个可替换的传输层后面，测试就是靠替掉它来跑完整流程的。加新的 API 调用时沿用这个结构，不要直接用 `urllib`。

## 许可证

MIT
