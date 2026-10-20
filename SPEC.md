# domainkit v2 规格:域名上线自动化

状态:已定稿。五项待确认项已由你拍板,记录在第 11 节,实现以本文为准。

## 1. 前提

这些是本工具成立的前提,已与你确认:

1. 站点是 S3 静态站,桶默认公开可读,源用 S3 网站端点或普通 HTTP 服务,不是 ALB/EC2 混合场景。
2. DNS 服务商支持 GoDaddy 和 Spaceship，未配置时兼容 GoDaddy；同一批任务可以混用。
3. GitLab 是自建或 gitlab.com,基准分支统一是 `dev`,分支名就是域名本身。
4. 一个域名对应一个 CloudFront 分配,也允许指定已有分配复用。
5. 裸域输入只申请和绑定 `www.<domain>`，不申请 `*.www.<domain>`，证书放在 us-east-1。
6. 支持环境变量和 GUI 凭证，GUI 可使用 Windows DPAPI 按当前用户加密保存。

### Spaceship 接入约束

只调用 `/v1/dns/records/{domain}` 的 GET、PUT、DELETE；默认 Endpoint 为 `https://spaceship.dev/api`，可配置。认证使用 `X-API-Key`、`X-API-Secret`。读取分页采用 take/skip；写入先读取并检查冲突，再以 force=false 仅提交需要新增的记录。PUT 为添加记录或更新 TTL，不重传无关记录及服务商字段；冲突拒绝，正确记录跳过；删除按名称、类型和目标精确匹配。

服务商优先级：单项 dns_provider > defaults.dns_provider > godaddy。GUI、CLI、批量共用工作流。Spaceship 裸域 301 明确显示为人工配置，不宣称已完成。自检不通过实际写入探测写权限。域名购买、续费、删除、转移、支付和联系人功能不在范围内。配置步骤和权限见 README 的 Spaceship DNS 节。

## 2. 目标

把「买完域名之后的所有手工步骤」收成一条命令或一次界面操作,并且能一次处理一批域名。

用户是运维本人。成功的样子:买完域名,填域名和源,点执行,回来看到证书已签发、CloudFront 已上线、GitLab 分支和 CI/CD 变量都已就位。

## 3. 功能清单

### F1 命名推导(已有)

域名单一输入,推导出全部下游名字。这是整个工具的地基,已实现并有测试。

| 推导项 | 规则 | 例子 |
| --- | --- | --- |
| GitLab 分支名 | 域名原样 | `livingtechnology.com` |
| CI/CD 变量键 | `CLOUDFRONT_` + 各级标签大写下划线连接 | `CLOUDFRONT_LIVINGTECHNOLOGY_COM` |
| 证书主体名 | 顶级注册域 + 通配 SAN | `example.com`, `*.example.com` |
| DNS 区域 | 可注册域(懂 `co.uk` 这类多段后缀) | `example.co.uk` |

### F2 ACM 证书(已有)

复用已存在且覆盖所需名字的证书;没有则用 DNS 验证方式申请;可选阻塞等待签发。

### F3 GoDaddy DNS(已有 + 新增自动写站点记录)

已有:把 ACM 的验证 CNAME 写进 GoDaddy 区域,幂等,值相同就不写。ACM 返回的是完整域名,GoDaddy 只接受相对名,工具自动做这一层换算。

新增:分配就绪后自动写站点记录,不再只给建议。

| 记录 | 做法 | 说明 |
| --- | --- | --- |
| `www.<域名>` | CNAME 指向分配的 `d***.cloudfront.net` | 真正承载流量的记录,也是 CloudFront 备用域名 |
| 根域名 | GoDaddy 域名转发到 `https://www.<域名>` | 301 永久跳转；若 GoDaddy forwarding API 不支持则提示手动设置 |

GoDaddy DNS 不支持根域名 CNAME,所以根域名走 GoDaddy 自带的域名转发功能,这与你手工的做法一致。转发同时启用 `masking=false`,即地址栏真实跳转而不是框架套壳。

### F4 CloudFront(新增创建能力)

这是这一轮的核心扩展。v1 只能往已有分配上挂域名,v2 可以创建。

创建时接受的选项:

| 选项 | 默认值 | 说明 |
| --- | --- | --- |
| `origin` | 必填 | 源域名,如 `site.s3-website-us-east-1.amazonaws.com` |
| `origin_type` | `s3-website` | `s3-website` / `custom` |
| `origin_path` | 空 | 源路径前缀 |
| `comment` | 自动生成 | 分配描述,尾部追加 `[domainkit:<域名>]` 标记 |
| `default_root_object` | `index.html` | |
| `price_class` | `PriceClass_All` | |
| `spa` | 关 | 开启则 403/404 重写到 `/index.html` 返回 200 |
| `compress` | 开 | |
| `viewer_protocol` | `redirect-to-https` | |

固定行为:裸域名输入时,CloudFront 备用域名使用 `www.<域名>`；证书只申请实际绑定到 CloudFront 的备用域名,默认即 `www.<域名>`。仅 GET/HEAD,缓存策略用 AWS 托管的 CachingOptimized,TLS 最低 `TLSv1.2_2021`,`sni-only`,HTTP/2 与 IPv6 开启。

计费方案对应控制台里的 **Choose a plan → Pay as you go**,也就是不启用 CloudFront 安全套餐(不建 WAF、不绑安全策略)。API 层面这就是「什么都不加」的默认形态,所以工具不发送任何 WAF 相关字段。

因为你的桶本来就是公开可读的,源统一按公开 HTTP 端点处理:`s3-website` 和 `custom` 都用 `CustomOriginConfig`,`s3-website` 固定走 HTTP 80(S3 网站端点不支持 HTTPS)。

**明确不做**:Origin Access Control / OAI 与配套的 S3 桶策略、Lambda@Edge、函数关联、WAF、日志投递、自定义缓存策略。这些留在控制台里微调。

### F5 GitLab(已有 + 新增多仓库)

已有:从 `dev` 建分支,设置 CI/CD 变量,都幂等。新增:每个域名可以指向不同的仓库,让「多域名多仓库」成立。

### F6 批量编排(新增)

一份 JSON 描述整批工作,`defaults` 给全局默认,`domains[]` 每项可覆盖任何默认字段。这一个结构同时表达「一批域名进一个仓库」和「多域名对多仓库」。

```json
{
  "defaults": {
    "gitlab_project": "group/frontend",
    "base_branch": "dev",
    "origin_type": "s3-website",
    "create_distribution": true,
    "wait_certificate": true
  },
  "domains": [
    { "domain": "example.com", "origin": "example.s3-website-us-east-1.amazonaws.com" },
    { "domain": "shop.example.com", "origin": "shop.s3-website-us-east-1.amazonaws.com",
      "gitlab_project": "group/shop", "spa": true },
    { "domain": "legacy.com", "distribution_id": "E1234567890ABC" }
  ]
}
```

必填 `domain`;要创建分配则必填 `origin`;给了 `distribution_id` 就跳过创建直接挂载。

失败策略:单个域名失败不中断整批,末尾汇总失败清单,退出码非零。

### F7 图形界面(新增)

tkinter/ttk 实现,零第三方依赖,`python -m domainkit.gui` 启动。

界面分两个页签。单域名页是一张表单:域名、源、源类型、GitLab 仓库、基准分支、几个开关,底下预演和执行两个按钮。批量页可以打开批次 JSON、在表格里看到每一行的域名和目标仓库、整批预演或执行,也能把表格存回 JSON。

下半屏是滚动日志,工作线程通过队列把日志送到主线程刷新,执行期间按钮禁用。凭证只读环境变量,界面里有一个连通性自检按钮,显示 AWS / GoDaddy / GitLab 是否可用,不显示密钥内容。

### F8 幂等与安全

这是最关键的设计约束,重复执行不能产生重复资源。

分配的发现顺序:显式 `distribution_id` → 别名匹配 → `Comment` 里的 `[domainkit:<域名>]` 标记 → GitLab 里已有的变量值。四条都没命中才算「不存在」。

**创建必须显式开启**(`--create-distribution` 或批次里 `create_distribution: true`)。没开启时找不到分配就跳过并提示,绝不隐式创建。

`plan` 与 `apply` 走同一条代码路径,`apply` 必须带 `--yes`,否则退出码 2。

## 4. 执行顺序

新分配的别名必须挂在一张已签发且覆盖该名字的证书上,所以顺序是被证书卡住的:

1. 申请或复用证书
2. 写 GoDaddy 验证记录
3. 创建分配(不带别名,先用 CloudFront 默认证书)
4. 等证书签发
5. 挂别名 + 换证书
6. 写站点 DNS 记录指向分配
7. GitLab 建分支 + 写变量

证书默认等待 600 秒（10 分钟）。超时停止后续 CloudFront、站点 DNS 和 GitLab 步骤，提示“等待验证，可稍后重试”，重新执行复用证书。CLI 和批量保留非成功状态，避免误报完成。需要更长的余量可用 `--wait-timeout` 调整。

## 5. 命令

```bash
python -m unittest discover -s tests -t .          # 测试
python -m domainkit names example.com              # 看推导结果,不联网
python -m domainkit doctor                         # 凭证与连通性自检
python -m domainkit plan example.com --origin site.s3-website-us-east-1.amazonaws.com --create-distribution
python -m domainkit apply --yes --wait example.com --origin site.s3-website-us-east-1.amazonaws.com --create-distribution
python -m domainkit batch batch.json               # 整批预演
python -m domainkit batch batch.json --yes --wait  # 整批执行
python -m domainkit variables --project group/repo # 查已有变量
python -m domainkit.gui                            # 图形界面
```

`--json` 是全局开关,放在子命令之前。

## 6. 目录结构

```
outputs/domainkit/
  SPEC.md, README.md, pyproject.toml, domainkit.example.json, batch.example.json
  domainkit/
    naming.py sigv4.py http.py errors.py     # 不动
    aws.py                                   # 加 create_distribution
    godaddy.py                               # 加站点记录写入
    gitlab.py config.py                      # 加多仓库支持
    workflow.py                              # 加发现与创建步骤
    batch.py                                 # 新增,批次解析与编排
    gui.py                                   # 新增,tkinter 界面
    cli.py                                   # 加 batch 子命令与创建参数
  tests/
    test_naming.py test_sigv4.py test_clients.py test_workflow.py test_cli.py
    test_batch.py                            # 新增
    support.py
```

## 7. 代码风格

沿用现有约定:`from __future__ import annotations`,数据结构用 frozen dataclass,所有远程调用经 `Transport` 接口以便注入假实现,每步返回 `StepResult` 汇报做了什么。

```python
def create_distribution(
    self,
    domain: str,
    origin_domain: str,
    *,
    origin_type: str = "s3-website",
    comment: str = "",
    spa: bool = False,
) -> Distribution:
    """Create a distribution with one origin and no aliases yet."""
```

## 8. 测试策略

`unittest`,离线,靠 `FakeTransport` 回放假响应。新增覆盖:创建分配生成的 XML 形状、创建后的响应解析、四级发现顺序、批次 defaults 合并与覆盖、单域名失败不影响整批、重跑不产生第二个分配。托管缓存策略 ID 写成常量并由测试固定。

GUI 不写自动化测试,tkinter 事件循环不值得为它搭夹具,改为手工验证启动、表单执行、日志刷新、按钮禁用恢复。

现有 88 个测试必须继续全绿。

## 9. 边界

**一定做**:创建前检查别名是否已被其他分配占用;批次为空报错;单域名超时只中断该域名;GUI 在后台线程执行;所有 AWS 调用走 SigV4。

**先问再做**:改动现有幂等判断;改 `Settings` 默认值;删 README 已有章节。

**绝不做**:改 `sigv4.py` 和 `http.py`(已通过 AWS 官方测试向量);界面或日志里显示密钥;引入 tkinter 之外的依赖;破坏现有 CLI 参数兼容性。

## 10. 成功标准

1. 一条 `apply --yes --wait` 命令能让一个新买的域名从零到上线,包含分配创建。
2. 同一条命令重跑两次,第二次全部报告 `exists`,不产生第二个分配、第二张证书、第二个分支。
3. 一份三域名两仓库的批次文件,执行后两个仓库里各自出现对应分支和变量。
4. 批次中一个域名失败,其余照常完成,退出码非零且末尾列出失败项。
5. `python -m domainkit.gui` 启动不闪退,点执行一秒内出现日志,执行期间按钮禁用。
6. 全量测试通过,新增逻辑有对应测试。

## 11. 已定稿的决策

原先挂着的待确认项已由你逐条拍板,以下是最终结论,实现以此为准。

**一、源与计费**。桶默认公开可读,所以源统一按公开 HTTP 端点处理,`s3-website` 与 `custom` 都用 `CustomOriginConfig`,不做 OAC / OAI,也不动 S3 桶策略。计费取 Pay as you go,API 层不发送任何 WAF 或安全套餐字段。

**二、站点 DNS 自动写**。工具直接落记录,不再只给建议。`www` 写 CNAME 指向分配域名;根域名尝试用 GoDaddy 域名转发跳 `https://www.<域名>`,类型 `REDIRECT_PERMANENT`(301),`masking=false`。若 GoDaddy forwarding API 返回不可用,该步骤给出手动提示并跳过,不让整条上线流程失败。

**三、证书等待默认 600 秒**。`--wait` 保留为显式开关，超时按等待验证处理，停止下游步骤，稍后重跑继续。

**四、验证记录名相对化**。ACM 给完整域名,GoDaddy 只认相对名,工具自动截掉区域后缀,apex 写 `@`。例如 `_c4c7d2d263470e12fb57e06bc0e7ebd2.www.oursunnyera.com.` 落到 GoDaddy 是 `_c4c7d2d263470e12fb57e06bc0e7ebd2.www`。

**五、批次文件只支持 JSON**。不做 CSV 入口,少一种格式少一处分歧。

## 12. 诚实的说明

这套代码从未对真实的 AWS、GoDaddy、GitLab 执行过,当前环境没有外网。全部验证来自单元测试和假传输层。上生产前建议先拿一个不重要的域名试跑一次。

## 13. GitLab 项目自动发现(计划)

### 13.1 目标

GUI 不再要求运维手工记住或复制 GitLab `Project ID`。用户只填写 GitLab 实例地址和 Access Token,工具自动验证 token、拉取该 token 可访问的项目列表,让用户选择目标仓库,然后把稳定的数字项目 ID 回填并保存。

成功的样子:在「凭证配置」里填好 `GitLab URL` 和 `Access Token`,点击「加载 GitLab 项目」,下拉框出现 `Ruby/Ruby-WebSite  (ID: 123)` 这类选项;选中后,凭证页和单域名页的 GitLab 项目字段都变成可直接执行的项目引用。

### 13.2 前提与假设

1. Token 本身不能反推出唯一项目,只能调用 GitLab API 查询它有权限访问的项目。
2. 默认从当前填写的 `GitLab URL` 查询,留空时使用 `https://gitlab.com`。
3. 项目发现优先保存数字 `id`,因为数字 ID 比 `namespace/project` 路径更稳定。
4. 如果 token 是 Project Access Token,它通常只能看到所属项目;如果是 Personal Access Token,可能看到很多项目。
5. 创建分支和写 CI/CD 变量仍需要足够权限;项目能被列出不代表一定能写变量。

### 13.3 功能范围

| 功能 | 做法 |
| --- | --- |
| 验证 GitLab token | 调 `GET /api/v4/user`,显示用户名或 bot 名称 |
| 搜索项目 | 调 `GET /api/v4/projects?membership=true&search=<关键字>&per_page=100&page=N` |
| 分页 | 读取 `X-Next-Page`,直到没有下一页 |
| 展示项目 | GUI 下拉/列表显示 `path_with_namespace  (ID: id, default: branch)` |
| 回填项目 | 选中后把数字 `id` 写入 `Project ID`,同时保留展示名方便识别 |
| 保存 | 凭证保存时保存 `GITLAB_PROJECT_ID=<数字ID>`;默认配置也可保存该值 |
| 自检 | 选中项目后继续使用现有 doctor 逻辑检查基准分支 `dev` 是否存在 |

### 13.4 GUI 交互

凭证配置页新增:

```text
GitLab URL:       [https://gitlab.example.com                 ]
Access Token:     [********************************              ] [显示]
项目搜索:         [Ruby-WebSite                                  ] [加载 GitLab 项目]
项目列表:         [Ruby/Ruby-WebSite (ID: 123, default: dev)      v]
Project ID:       [123                                           ]
```

单域名页的「GitLab 项目」字段继续保留,但可以从凭证页自动带入。后续如果要多仓库批量执行,批量 JSON 仍可覆盖每个域名自己的 `gitlab_project`。

### 13.5 API 与错误处理

新增 GitLab 客户端方法:

```python
def get_current_user(self) -> Dict[str, Any]: ...

def list_accessible_projects(
    self,
    *,
    search: str = "",
    membership: bool = True,
) -> List[Dict[str, Any]]: ...
```

错误提示规则:

| 场景 | GUI 提示 |
| --- | --- |
| 401 | Token 无效,或 GitLab URL 与 token 所属实例不匹配 |
| 403 | Token 有效但权限不足 |
| 连接失败 | GitLab URL 不可访问或网络失败 |
| 0 个项目 | Token 可用,但没有找到可访问项目;换搜索词或检查 token 权限 |
| 项目无 `dev` 分支 | 项目找到,但基准分支不存在;不能继续自动建域名分支 |

### 13.6 权限建议

最小可用权限分两层:

| 用途 | 建议权限 |
| --- | --- |
| 只加载项目列表和做自检 | `read_api` 或 `api` |
| 创建分支、写 CI/CD 变量 | `api` + Maintainer 角色;分支创建还需要仓库写权限 |

实际使用建议仍然是给工具 token `api` 权限,并确保该 token 对目标项目至少是 Maintainer。这样发现项目、创建分支、写变量是一条链路,不会出现前面能查、后面不能写的半成功状态。

### 13.7 边界

**一定做**:不在日志里打印完整 token;项目列表分页;保存数字 ID;保留手填 Project ID 入口;加载项目时不修改 AWS/GoDaddy 配置。

**先问再做**:自动修改批量 JSON 里的项目字段;删除手填 Project ID;把 GitLab 项目列表缓存到磁盘。

**绝不做**:从 token 文本本身解析项目;把 token 或项目列表上传到外部服务;没有用户选择就自动猜一个项目执行。

### 13.8 验收标准

1. 只填 `GitLab URL` 和 `Access Token`,点击加载项目后能看到 token 可访问项目列表。
2. 选择项目后,`Project ID` 自动变成数字 ID,并能通过现有「测试连接」检查项目和 `dev` 分支。
3. 保存凭证后重启 GUI,项目 ID 能恢复,token 仍按本机 DPAPI 加密保存。
4. GitLab 项目很多时不会漏第一页之后的项目。
5. 401/403/空结果都有中文错误提示,不会静默失败。
6. 原来手填 `Ruby/Ruby-WebSite` 或数字 ID 的方式继续可用。

### 13.9 实施计划

1. 扩展 `GitLabClient`:支持无项目 ID 的实例级客户端,增加当前用户和项目列表 API。
2. 增加单元测试:覆盖项目 ID 规范化、分页、搜索参数、401/403 错误传播。
3. 改 GUI 凭证页:增加搜索框、加载按钮、项目下拉框、选择后回填 Project ID。
4. 改凭证保存/默认配置:继续保存 `GITLAB_PROJECT_ID`,优先保存数字 ID。
5. 手工验证:用真实 GitLab token 加载项目、选择 `Ruby/Ruby-WebSite`、运行自检。

## 14. GUI 源站预设(计划)

### 14.1 目标

运维经常把多个域名指向同一批 S3 源站,只是 `origin_path` 不同。GUI 单域名页需要支持保存多条常用源站配置,后续通过下拉或搜索选择,避免重复输入源域名和路径。

### 14.2 范围

每条源站预设包含三项:

| 字段 | 说明 |
| --- | --- |
| `origin` | 源域名,如 `example-bucket.s3.ap-south-1.amazonaws.com` |
| `origin_type` | `s3-website` 或 `custom` |
| `origin_path` | 源路径前缀,如 `/website/ruby-rummy.com` |

预设保存在 `%LOCALAPPDATA%\\domainkit\\gui-defaults.json` 的 `origin_presets` 数组里。它不是密钥,不走 DPAPI 加密。历史单条默认值 `origin` / `origin_type` / `origin_path` 会自动作为第一条预设导入。

### 14.3 GUI 交互

单域名页新增:

```text
源站预设: [example-bucket.s3.amazonaws.com/website/a.com (s3-website) v]
预设搜索: [ruby-rummy                                      ] [筛选]
          [保存当前源站] [删除所选源站]
源域名:   [example-bucket.s3.amazonaws.com                     ]
源类型:   [s3-website                                      v]
源路径:   [/website/ruby-rummy.com                          ]
```

选择预设会回填源域名、源类型、源路径。若下拉里没有需要的配置,用户手动输入三项后点击「保存当前源站」即可新增;重复保存同一条会把它移动到列表顶部,最多保留 50 条。

### 14.4 边界

**一定做**:保存多条源站预设;按源域名或源路径搜索;选择后回填三项;保留当前单条默认值兼容。

**不做**:保存 Distribution ID;把预设写进批量 JSON;把源站预设加密;自动从 AWS 反查源站列表。

## 15. GUI 体验优化(草案)

### 15.1 假设

1. 继续使用 Tkinter/ttk,保持零第三方依赖,不改成 Web / Electron / PyQt。
2. 目标用户是运维本人,重点是减少误操作、减少重复填写、让执行状态更清楚,不是做营销式漂亮界面。
3. 保留现有 CLI、配置文件、凭证加密、幂等流程,GUI 只是更好地组织这些能力。
4. 本轮先优化单窗口桌面工具,不引入账号体系、团队共享配置、远程服务。
5. 文案继续中文优先,日志和错误提示要可直接指导下一步操作。

### 15.2 Objective

把当前“表单堆叠型 GUI”优化为运维工作台。用户打开后能按自然流程完成:配置凭证 -> 选择项目和源站 -> 预演 -> 执行 -> 查看每一步结果。成功的样子是:不用翻 README 也能知道下一步该点什么;预演结果能看懂是否会新建资源;执行失败时能定位 AWS、GoDaddy、GitLab 哪一步失败。

### 15.3 Tech Stack

| 项 | 决策 |
| --- | --- |
| GUI | Tkinter + ttk |
| 主题 | 继续使用系统控件,但统一 padding、分组、按钮层级、状态颜色 |
| 配置 | 继续使用 `%LOCALAPPDATA%\\domainkit` 下的凭证和默认配置文件 |
| 网络 | 继续复用现有客户端和 workflow,不在 GUI 里直接写 API 调用 |
| 测试 | 可测试的状态/格式化/配置函数写单元测试;Tk 主循环做手工验证 |

### 15.4 Commands

```powershell
cd "D:\\进程文件\\自动化运维工具\\domainkit"
.\\domainkit.cmd gui
& "python" -m py_compile domainkit\\gui.py
& "python" -m unittest discover -s tests -t . -q
```

### 15.5 Project Structure

本轮优先控制改动范围:

```text
domainkit/gui.py          # GUI 布局、状态展示、表单组织、日志视图
tests/test_gui.py         # 不依赖 Tk 主循环的 GUI 辅助函数测试
README.md                 # 使用说明和截图式流程文字
SPEC.md                   # 本规格与后续决策记录
```

如果 `gui.py` 继续膨胀,再拆分 `domainkit/gui_state.py` 或 `domainkit/gui_widgets.py`;拆分前必须保证行为不变并有测试兜住辅助逻辑。

### 15.6 用户流程

优化后的主流程分成四块,而不是让用户在三个页签里来回找:

| 区域 | 内容 | 目标 |
| --- | --- | --- |
| 顶部状态栏 | 凭证状态、当前 GitLab 项目、默认源站、运行状态 | 一眼知道工具是否准备好 |
| 左侧导航 | 单域名上线、批量上线、凭证与项目、源站预设、日志 | 降低页签堆叠感 |
| 主工作区 | 当前任务表单和预演摘要 | 填写和确认集中在一处 |
| 底部日志/结果 | 分步骤结果、错误、复制按钮 | 执行后能追踪和复盘 |

### 15.7 功能范围

#### A. 主界面布局

1. 窗口默认增大到更适合表单的尺寸,例如 `1100x760`。
2. 用左右分栏替代纯 Notebook:左侧为导航,右侧为当前页面。
3. 表单区按“基础信息 / 源站与 CloudFront / GitLab / 执行选项”分组。
4. 常用字段放上方:域名、GitLab 项目、源站预设、源路径、创建分配、等待证书。
5. 不常用字段收进“高级选项”:Distribution ID、跳过 DNS、跳过 GitLab、跳过站点 DNS。

#### B. 凭证与项目体验

1. 顶部状态栏显示 AWS、GoDaddy、GitLab 三项状态:未配置、待自检、正常、失败。
2. 「测试连接」后把 doctor 结果转成状态徽标,不只写日志。
3. GitLab 项目加载后展示项目路径、数字 ID、默认分支,选中后同步到单域名页。
4. 敏感字段继续默认隐藏,日志只显示掩码摘要。

#### C. 单域名执行体验

1. 输入域名后实时显示推导结果:服务域名 `www.<domain>`、分支名、变量名、证书名。
2. 「预演」结果用列表展示每一步:证书、DNS 验证、CloudFront、站点 DNS、apex 转发、GitLab。
3. 每一步显示状态:将创建、将复用、已存在、跳过、失败。
4. 「执行」按钮只在域名、项目、源站等必要信息满足时可用;否则给出缺失项提示。
5. 执行前确认框列出将真实变更的服务,而不是泛泛说 AWS/GoDaddy/GitLab。

#### D. 源站预设体验

1. 源站预设改成可搜索 Combobox + 管理列表。
2. 预设列表至少显示源域名、源路径、源类型、最近使用时间。
3. 保存当前源站时自动去重并移动到顶部。
4. 不保存 Distribution ID,避免跨域名误复用。

#### E. 批量体验

1. 批量页表格显示域名、项目、源站、源路径、创建分配、SPA、状态。
2. 加载 JSON 后先做本地校验,把缺字段行标红或在状态列提示。
3. 批量预演/执行后每个域名一行状态,双击或选中可查看详细步骤。
4. 保留原 JSON 文件格式,不强制用户通过 GUI 编辑 JSON。

#### F. 日志与结果

1. 日志保留完整文本,但增加“清空日志”“复制日志”“保存日志”按钮。
2. 执行结果单独做结构化摘要,减少用户从大段日志里找结论。
3. 错误提示尽量给下一步,例如 GoDaddy apex 转发 404 时提示去控制台手动设置 301。

### 15.8 Code Style

GUI 代码保持小函数拆分,事件处理只做收集表单和调度任务,不要把业务逻辑写进 Tk 事件里。

```python
def _build_single_page(self) -> None:
    self._build_single_basic_section(parent)
    self._build_single_origin_section(parent)
    self._build_single_gitlab_section(parent)
    self._build_single_action_section(parent)

def _format_step_status(step: StepResult) -> str:
    return "将创建" if step.action == "create" else "已存在"
```

### 15.9 Testing Strategy

1. 单元测试覆盖纯函数:状态格式化、预演结果摘要、源站预设排序、缺失字段校验。
2. `py_compile` 覆盖 Tkinter 代码语法和导入。
3. 全量 `unittest` 必须通过。
4. GUI 手工验证:启动、不闪退、切换页面、保存凭证、加载 GitLab 项目、源站预设选择、单域名预演、按钮运行中禁用、日志复制/清空。

### 15.10 Boundaries

**Always**:保持 CLI 兼容;不显示完整密钥;不改变 workflow 幂等逻辑;真实执行前仍二次确认;文案中文优先。

**Ask first**:引入第三方 GUI 库;修改批量 JSON 格式;把配置拆成多个新文件;自动执行 GoDaddy 购买域名;改变默认创建/跳过策略。

**Never**:在日志、README、截图中写真实密钥;保存 Distribution ID 到默认配置;绕过 `--yes`/确认语义;为了界面优化重写 AWS/GitLab/GoDaddy 客户端。

### 15.11 Success Criteria

1. 新用户打开 GUI 后能按顶部状态和页面顺序完成一次单域名预演。
2. 单域名页首屏能看到最常用字段,不需要滚动才能找到执行按钮。
3. 高风险选项被放进高级区,默认不干扰常规流程。
4. 预演和执行结果都有结构化摘要,能明确看出新建/复用/跳过/失败。
5. 凭证自检结果不只出现在日志里,还同步到可见状态区。
6. 全量测试通过,新增 GUI 辅助逻辑有单元测试。

### 15.12 Decisions

1. 本轮继续使用 Tkinter 做“工作台式”优化,不切 Web GUI。
2. 接受左侧导航替代顶部页签。
3. 本轮优先打磨单域名上线;批量页保留入口和基础表格,完整批量体验放下一轮。
4. 顶部状态栏显示凭证自检结果和当前 GitLab 项目,单域名页保留项目输入框。
5. 最近执行域名记录暂不实现,先把预演/执行摘要和日志操作做好。

### 15.13 Implementation Plan

1. 先重构 GUI 页面骨架:顶部状态栏 + 左侧导航 + 主工作区 + 底部日志。
2. 把单域名页按业务分组重排,加入实时命名预览和高级选项折叠区。
3. 把 doctor 结果、预演结果、执行结果做成结构化摘要。
4. 优化源站预设管理区,加入最近使用时间和更清晰的选择/删除行为。
5. 优化批量页表格和本地校验提示。
6. 补 README、跑测试、手工启动 GUI 验证。

### 15.14 Tasks

- [x] Task: GUI 骨架重排
  - Acceptance: 启动后显示顶部状态栏、左侧导航、主工作区、底部日志;原三个页面能力都能进入。
  - Verify: `py_compile`, 手工启动 GUI。
  - Files: `domainkit/gui.py`

- [x] Task: 单域名页重排与命名预览
  - Acceptance: 常用字段聚合,输入域名后显示服务域名、分支、变量名、证书名。
  - Verify: 新增纯函数测试 + 手工输入域名。
  - Files: `domainkit/gui.py`, `tests/test_gui.py`

- [x] Task: 凭证状态和 GitLab 项目体验
  - Acceptance: doctor 结果同步到状态栏;GitLab 项目选择同步到单域名页。
  - Verify: 假状态格式化测试 + 手工自检。
  - Files: `domainkit/gui.py`, `tests/test_gui.py`

- [x] Task: 预演/执行结果摘要
  - Acceptance: 日志保留,同时展示按步骤分组的摘要;失败步骤醒目。
  - Verify: 步骤格式化测试 + GUI 预演。
  - Files: `domainkit/gui.py`, `tests/test_gui.py`

- [ ] Task: 批量页基础优化
  - Acceptance: 批量表格显示更多关键列,本地校验错误能在表格里看到。
  - Verify: 批量 JSON 加载手工验证。
  - Files: `domainkit/gui.py`, `tests/test_gui.py`

- [ ] Task: 文档与验收
  - Acceptance: README 描述新 GUI 使用流程;全量测试通过。
  - Verify: `python -m unittest discover -s tests -t . -q`
  - Files: `README.md`, `SPEC.md`
