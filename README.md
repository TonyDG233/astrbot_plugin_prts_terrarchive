# astrbot_plugin_prts_terrarchive · PRTS 泰拉档案

PRTS 泰拉档案（`astrbot_plugin_prts_terrarchive`）是面向 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 的本地语料检索与研究插件，**为非官方社区移植项目**，移植自 [HTian-qwq/prts-terrarchive](https://github.com/HTian-qwq/prts-terrarchive)；本仓库地址：[TonyDG233/astrbot_plugin_prts_terrarchive](https://github.com/TonyDG233/astrbot_plugin_prts_terrarchive)。

本插件允许大语言模型在对话中实时检索并精确查阅《明日方舟》与《明日方舟：终末地》的官方剧情原文、干员档案、语音资料、Wiki 整理条目、泰拉年表以及终末地官方多语言对照文本，为剧情考据、设定答疑、角色对话与双游戏关系研究提供可核验、不串库的本地权威证据支撑。

---

## 功能特性

### 1. 四大核心 LLM 检索与读取工具
- **`prts_search`（本地结构化全文检索）**：
  支持像 grep 一样极速检索本地语料。支持指定游戏（明日方舟 / 终末地）、资料类型（剧情、干员密录、官方资料、Wiki 整理等）、内容形式（对话、广播、过场、SNS 聊天等）、角色归属、活动名称、篇章名称、说话人与 Wiki 标签字段。搜索命中立即返回原行及上下各一行上下文，并按文档结构化归并。下一页检索通过 `next_after` 锚点原样传回 `after`，稳定可预测。
- **`prts_read`（原文精准定位与连读）**：
  支持直接通过玩家可见的稳定定位符读取原文。明日方舟支持关卡代号（`stage_code`，多篇时加 `story_part`）、干员密录（`character_name` + `record_name` + `segment`）、角色官方资料（`material` 支持 profile/module/voice/skin 等）；整活动或终末地任务支持合集连续阅读模式（`mode="activity"` 或 `mode="collection"`）；当存在同名篇章歧义时自动返回 `document_uid` 进行精确消歧；长文与合集翻页均原样提交 `page.continuation`。
- **`prts_timeline`（泰拉年表检索）**：
  基于 PRTS Wiki《泰拉年表》本地投影，支持按活动名、年份范围与实体名（支持实体别名自动展开映射）检索历史大事件与剧情发生节点。返回精确年份、年表出处标记与事件内容。
- **`prts_i18n`（终末地官方多语言对照）**：
  查询《明日方舟：终末地》官方跨语言文本（支持 CN、EN、JP、KR），支持通过中文原句、专名反查对应语言官方翻译，或直接通过标题/篇章行号定位多语言段落。严禁非官方机翻，忠实展现官方本地化用词。

### 2. `/prts` 管理命令组
内置直观的管理指令，支持群聊与私聊环境下的语料运维：
- `/prts 状态`：查看语料库安装状态、当前激活版本、已启用游戏与缓存分片统计。
- `/prts 检查更新`：异步向官方源检查远端最新 release 版本（仅检查，不主动下载）。
- `/prts 更新 [release_id]`：管理员指令，自动下载指定版本或最新语料包，并执行原子校验与激活。
- `/prts 版本`：列出本地已下载并就绪的全部语料版本。
- `/prts 激活 <release_id>`：管理员指令，将指定本地版本切换为当前生效版本并热重载。
- `/prts 删除 <release_id>`：管理员指令，清理已废弃的历史版本目录（受保护机制约束，禁止删除当前活跃版本）。
- `/prts 帮助`：查看指令帮助说明。

### 3. 动态实体上下文注入（Retrieval Context）
在每次用户向大模型提问时，插件可自动预识别消息中包含的角色名、活动名及设定实体，生成 `<prts:retrieval-context>` 提示块并注入到当前对话上下文（通过 AstrBot `extra_user_content_parts` 机制）。提示明确标注实体所属游戏，有效避免大模型因双游戏同名实体或相似概念导致的串库误判。

---

## 前置条件与安装

### 前置条件
1. **AstrBot 版本**：要求 AstrBot ≥ 4.24.2（原生支持插件内置 Skill 自动发现与加载）。
2. **LLM 模型要求**：必须使用**支持 Function Calling（工具调用）**的模型（如 GPT-4o、Claude 3.5 Sonnet、DeepSeek-V3/R1、Qwen-2.5-72B 等），且在 AstrBot 配置中启用了函数调用功能。

### 安装步骤
1. **放置插件**：将本插件目录复制或克隆至 AstrBot 的 `data/plugins/` 目录下，文件夹命名为 `astrbot_plugin_prts_terrarchive`。
2. **安装依赖**：
   在插件根目录下执行：
   ```bash
   pip install -r requirements.txt
   ```
   依赖包括 `aiohttp`（异步下载与更新）等。若暂未安装 `aiohttp`，插件仍可加载运行已有语料，但版本更新命令将被禁用。
3. **重启或重载**：在 AstrBot 控制台重载插件，或重启 AstrBot 进程。

---

## 首次使用

插件初次安装后，本地尚未包含语料包（语料包体积较大，不随插件代码仓库分发）。

1. 发送管理员指令：
   ```text
   /prts 更新
   ```
   插件将自动从默认源（优先国内 ModelScope 镜像，失败自动故障转移至 prts.chat 官方站点）下载最新版本的语料包。
2. 下载过程中会自动流式写入临时目录，并对清单、索引与正文分片执行严格的 SHA-256 完整性校验。
3. 校验完成后，插件将原子激活该版本并自动热重载内存缓存。此时发送 `/prts 状态` 即可看到当前语料库已就绪，大模型即可在对话中调用工具检索。

---

## 管理台配置项说明

可在 AstrBot Web 管理面板的“插件配置”中对本插件进行个性化调整。配置项及默认值如下：

| 配置项 | 类型 | 默认值 | 作用与说明 |
| :--- | :--- | :--- | :--- |
| `enabled_games` | list | `["arknights", "endfield"]` | 启用的游戏资料库。可选 `arknights`（明日方舟）、`endfield`（明日方舟：终末地）。未启用的游戏不会注册到索引中。 |
| `releases_dir` | string | `""` | 语料存储目录。留空时使用标准路径 `data/plugin_data/astrbot_plugin_prts_terrarchive/releases`。 |
| `site_base_url` | string | `https://prts.chat` | 官方元数据与备用下载源站点地址。可信元数据始终以此为基准校验。 |
| `download_order` | list | `["modelscope", "site"]` | 下载源优先级列表。优先从首选源拉取分片，遇到网络异常时自动降级到下一源。 |
| `metadata_source` | string | `"auto"` | 可信元数据来源：`site`=仅 prts.chat；`mirror`=仅 ModelScope 镜像（海外 VPS 适用）；`auto`=优先 prts.chat，网络不可达时自动回退 ModelScope 镜像。镜像模式的 `data_version` 为本地按同一公式重算值，官方声明值记录在 `mirror_declared_data_version`。 |
| `pinned_release` | string | `""` | 固定使用的版本号（release_id）。设置且本地已存在该版本时，启动时不进行联网请求，实现绝对零联网纯离线运行。 |
| `auto_check_update` | bool | `false` | 启动后是否在后台静默检查远端是否有新版本语料（仅提示，不自动下载与切换）。 |
| `auto_update` | bool | `false` | 检测到新语料时自动下载并激活（后台按 6 小时周期检查）；开始/完成/失败状态写入 AstrBot 主日志。开启后无需另开 `auto_check_update`。 |
| `auto_install_on_start`| bool | `false` | 启动时若检测到本地未安装任何语料包，是否自动下载并安装最新版本。 |
| `enable_search_tool` | bool | `true` | 是否向 LLM 注册 `prts_search` 检索工具。 |
| `enable_read_tool` | bool | `true` | 是否向 LLM 注册 `prts_read` 阅读工具。 |
| `enable_timeline_tool` | bool | `true` | 是否向 LLM 注册 `prts_timeline` 泰拉年表工具。 |
| `enable_i18n_tool` | bool | `true` | 是否向 LLM 注册 `prts_i18n` 终末地多语言工具。 |
| `inject_entity_context`| bool | `true` | 是否在用户提问前注入 `<prts:retrieval-context>` 实体识别提示。 |
| `command_enabled` | bool | `true` | 是否启用 `/prts` 系列管理命令。 |
| `content_cache_mb` | int | `64` | 正文分片（JSONL.gz）LRU 解压缓存上限，单位 MiB。 |
| `index_cache_mb` | int | `32` | 检索倒排索引分片 LRU 缓存上限，单位 MiB。 |

---

## 数据来源与存储位置

### 存储路径
- 语料包默认存储在 AstrBot 规范的数据目录下：
  ```text
  data/plugin_data/astrbot_plugin_prts_terrarchive/releases
  ```
- 语料目录与插件代码目录完全分离，升级插件代码或重装插件时**绝不会**破坏或误删已下载的历史语料数据。
- 目录结构规范：
  ```text
  releases/
  ├── current.json                          # 当前活跃版本的指针元数据
  └── <release_id>/                         # 版本化语料目录
      ├── release-manifest.json             # 整包清单与全局签名
      ├── <pack_id>/pack-manifest.json      # 分包清单
      ├── shards/*.jsonl.gz                 # 压缩分片正文数据
      ├── search-index/*.bin.gz             # 压缩倒排与前缀检索索引
      ├── catalog/*.jsonl.gz                # 目录与结构元数据
      └── localization/*.jsonl.gz           # 多语言对照表（可选附件）
  ```

### 数据来源
语料来自 PRTS.chat 预构建发布的结构化归档，涵盖官方游戏数据解包提取、社区精细审校的 Wiki 词条与《泰拉年表》结构化投影。

---

## 离线运行与版本固定

本插件原生支持**完全离线**部署环境：
1. **离线迁移**：可以在有网机器上通过 `/prts 更新` 下载语料后，将整个 `releases/` 目录拷贝到离线服务器的对应数据路径下。
2. **固定版本（Pinning）**：在管理台将 `pinned_release` 配置为对应的 `release_id`（例如 `2026-09-20-r1`），插件在启动和运行时将完全跳过网络校验与远端探测，实现安全、封闭的纯局域网/单机稳定运行。

---

## 引用规范

为了保证回答严谨可考，并防止大模型向最终用户暴露底层工程内部细节，工具与 Skill 严格约定了引用规范：
- **官方剧情原文**：必须采用工具返回的公开标题进行引用，格式为：
  ```text
  《完整展示标题》第 N 行
  ```
  例如：《孤星 - CW-10 纯白》第 142 行。
- **Wiki 与官方资料**：直接使用用户可见的展示名与对应段落（如《凯尔希》干员档案·基础档案）。
- **隐私与安全保护**：严禁向用户输出底层内部标识，如 `document_uid`、`source_ref`、`text_id`、`continuation`、`source_marker` 或内部存储路径。

---

## 已知实现差异

本插件为基于 Python 与 AstrBot 体系的独立全新实现，与原 Node.js / Cordis 架构版本存在以下实现差异：
1. **排序规则**：底层搜索结果的展示名排序采用 Python 标准自然排序（支持多级字段平手决胜），未刻意复刻 Node.js 中特定的 `zh-CN localeCompare` 排序行为。
2. **游标与分页模型**：完全废弃了早期遗留的 cursor 游标体系，搜索统一采用天然与版本绑定的 `next_after` 锚点，正文长文阅读统一采用 `page.continuation`，状态更确定、无外部游标状态泄露风险。
3. **纯本地运行架构**：本地端口专注于完全离线与高性能本地索引，移除了云端检索、远程调试探针等依赖外部服务的相关逻辑。
4. **全量分片下载与完整性校验**：当前采用整包分片流式下载与完整性校验，未实现增量 delta 差分补丁下载。

---

## 常见问题（FAQ）

### Q1: 模型调用工具时提示“语料未安装”或“未找到有效语料库”？
A: 说明本地尚未下载语料包。请先使用管理员账号发送命令 `/prts 更新`。若当前处于纯离线环境，请手动将构建好的 release 文件夹放置于 `data/plugin_data/astrbot_plugin_prts_terrarchive/releases` 并包含有效的 `current.json`。

### Q2: 下载语料包时网络超时或失败？
A: 插件默认配置了双下载源。若 ModelScope 访问不稳定，可在管理台调整 `download_order` 配置或检查服务器到 `prts.chat` 的网络连通性。也可以手动下载语料压缩包解压至数据目录。海外服务器若无法访问 `prts.chat` 域名，保持 `metadata_source` 为默认 `auto` 即可自动改用 ModelScope 元数据回退（也可显式设为 `mirror`）。

### Q3: 磁盘空间与内存占用如何？
A: 完整语料包（明日方舟 + 终末地双游戏）占用磁盘约 300MB ~ 800MB（根据是否包含多语言包而定）。运行期正文与索引均采用 LRU 内存分片缓存，分别受 `content_cache_mb`（默认 64MB）与 `index_cache_mb`（默认 32MB）严格限制，对小内存服务器极其友好。

---

## 上游项目与致谢

- 项目性质：非官方社区移植项目，与游戏版权方、PRTS Wiki 及其官方无关。
- 本仓库：[TonyDG233/astrbot_plugin_prts_terrarchive](https://github.com/TonyDG233/astrbot_plugin_prts_terrarchive)
- 上游项目：[HTian-qwq/prts-terrarchive](https://github.com/HTian-qwq/prts-terrarchive)
- 协议：[MIT License](https://github.com/HTian-qwq/prts-terrarchive/blob/main/LICENSE)
- 感谢 PRTS.chat 团队及明日方舟、终末地社区对泰拉大陆资料的持续整理与奉献。
