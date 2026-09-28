---
name: prts-retrieval
description: PRTS 本地资料检索总入口；提供 prts_search、prts_read、prts_timeline、prts_i18n 四大工具的标准参数契约、过滤与分页规则、六步检索工作流及单篇/合集读取定位规范。
---

# PRTS 资料检索总入口

## 一、工作原则与证据标准

1. **运行时范围确认**：先确认每轮会话注入的 `<prts:retrieval-context>` 实体提示块。只查询当前已启用的资料库；若问题涉及未启用游戏，明确说明资料边界，不得把未启用游戏或未命中的情况写成“不存在”。
2. **证据层级与核验要求**：
   - **官方剧情原文（最高）**：`story` / `operator_record` / `original_story`，最适合支持原句台词、行动细节、场景还原与因果推断。整理性材料中即使带引号也不能直接充当原文明示事实；实质冲突时必须回读原文。
   - **官方结构化资料**：`character_profile` / `character_voice` / `character_module` / `archive`，最适合支持官方身份、设定定义与固定档案。
   - **整理性资料（导航与概括）**：Wiki、实体图谱、时间线，用于概括、人物清单、活动脉络与候选发现。
3. **回答与引用规范**：
   - 先直接回答问题，再给出必要依据；区分“资料明示的事实”、“整理性概括”与“推断”。
   - 原文引用格式必须采用工具返回的 `《完整展示标题》第 N 行`；其他资料使用用户可见标题。
   - 严禁向用户输出内部标识符（如 `source_ref`、`document_id`、`document_uid`、`entity_id`、`continuation`、`source_marker` 或内部文件路径）。
   - 外网搜索仅限于处理现实信息、时效公告或用户明确要求的外部内容，绝不能静默替代游戏内证据。

---

## 二、四大工具契约与使用规范

### 1. `prts_search`（本地结构化字面检索）
- **功能定位**：本地精确字面全文检索，用于发现候选篇章、原句定位或结构化概括。不接收完整长句问题。
- **支持参数**：
  - `query`（string）：检索词。默认连续字面匹配；确有通配需求时可使用受限正则（`match_mode:"regex"`）。
  - `games`（list[str]）：指定游戏范围，可选 `["arknights"]`、`["endfield"]`。明确单游戏时必须显式指定。
  - `resource_types`（list[str]）：限定资料类型（同一数组内 OR，不同字段间 AND）。
  - `content_types`（list[str]）：限定内容表现形式（终末地专长：`dialogue`、`cutscene`、`radio`、`remote_comm`、`black_screen`、`environment_talk`、`sns_topic`、`sns_chat`、`narration`）。
  - `collection_names`（list[str]）：上级集合（方舟活动名或终末地任务集合名）。
  - `character_names`（list[str]）：资料所属角色名。
  - `speakers`（list[str]）：台词亲口说话人名（与 `entity_names` 区分，查询亲口所说必填 `speakers`）。
  - `entity_names`（list[str]）：正文或结构化关系中出现的实体名。
  - `activity_names`（list[str]）：所属活动名。
  - `story_names`（list[str]）：所属篇章名。
  - `wiki_sections`（list[str]）：Wiki 字段标签名（如 `["剧情总结"]`、`["简要介绍"]`）。
  - `context_terms`（list[str]）：同篇共现上下文词。
  - `after`（object）：分页锚点。
- **高级用法**：
  - **列出目录/人物清单**：省略 `query`，只传 `character_names` 或 `collection_names`，配合 1~2 个 `resource_types`。
  - **提取单个 Wiki 字段**：省略 `query`，传目标 `character_names` + `resource_types:["character_wiki"]` + 单个 `wiki_sections:["简要介绍"]`，搜索会直接返回完整字段，无需全篇检索。
  - **分页规则**：保留全部原查询条件，将上一页返回的 `page.next_after` 原样填入 `after`；直到 `page.exhausted=true` 表示穷尽。

### 2. `prts_read`（稳定定位与连续阅读）
- **功能定位**：按用户可见标识直接阅读原文或 Wiki，支持定点上下文、连续分页与整活动通读。
- **优先定位器规则（满足时直接直读，无需先调 search）**：
  - **关卡剧情**：`{stage_code: "15-17"}`；同关卡存在多篇剧情时加 `story_part: "before"|"after"|"story"`。
  - **干员密录**：`{character_name: "凯尔希", record_name: "遗老"}`；多段密录加整数 `segment: 2`。
  - **干员官方资料**：`{character_name: "凯尔希", material: "profile"}`（支持 `profile` 档案、`module` 模组、`voice` 语音、`skin` 时装、`recruitment` 合同、`potential` 信物）。
  - **整活动通读（方舟）**：`{activity_name: "孤星", mode: "activity"}`。
  - **整任务通读（终末地）**：`{collection_name: "武陵特厨", mode: "collection"}`（可加 `content_types` 收窄）。
  - **定点上下文**：`{title: "展示标题", line: 101, before: 3, after: 3}`。
  - **Wiki 字段阅读**：`{title: "凯尔希", section: "简要介绍"}`。
  - **其他资料全文**：`{title: "展示标题", mode: "document"}`。
- **消歧与续读**：
  - 遇到同名篇章歧义时，搜索结果中若提供 `document_uid`，直接用 `{document_uid, line: 101}` 或 `{document_uid, mode: "document"}` 读取，不得同时传 `title`。
  - 续读统一使用上一轮结果返回的 `page.continuation` 传给下一步调用。单篇使用 `line`，合集使用 `position`。
  - 输出限制字段 `max_lines` / `max_chars` 仅控制单次返回量，不能替代 `line` / `section` / `mode:"document"` 作为读取方式。

### 3. `prts_timeline`（泰拉年表事件检索）
- **功能定位**：仅用于《明日方舟》结构化年表检索与事件先后考证。
- **支持参数**：`query`、`activity_names`、`entity_names`（自动展开别名图谱）、`year_start`、`year_end`、`max_results`。
- **边界说明**：年表为整理性证据，具体事件因果和对话细节仍需回读 `story` 原文。该工具不覆盖《终末地》。

### 4. `prts_i18n`（终末地官方多语言对照反查）
- **功能定位**：查询终末地官方多语言文本，严禁用于机械生成翻译。
- **主定位（四选一）**：`query`（原句/名称）、`title`、`document_uid`、`text_ids`。
- **必选参数**：`languages: ["EN", "JP"]`（支持 1~4 种，可选 `EN`/`JP`/`KR`）。
- **说明**：支持反查，`source_language` 默认 `CN`。长文本分块展示，多语言按字符各自索引，续读使用 `page.continuation`。

---

## 三、推荐标准六步检索流程

```mermaid
flowchart TD
    A[1. 判断问题类型与游戏归属] --> B[2. 选择最短入口]
    B --> C{是否具备稳定定位器?}
    C -- 是: stage_code / 密录 / 资料 --> D[直接 prts_read]
    C -- 否: 专名 / 原句 / 概念 --> E[带窄类型 prts_search]
    D --> F[3. 检查首轮结果]
    E --> F
    F --> G{证据是否充足且无实质冲突?}
    G -- 是 --> H[6. 停止并规范引用回答]
    G -- 否 --> I[4. 精准收窄 / 回读原文上下文]
    I --> J{是否遭遇异常或零命中?}
    J -- 是 --> K[5. 触发排障自愈]
    J -- 否 --> F
    K --> F
```

1. **判断问题和范围**：先确定属于方舟、终末地还是跨游戏。明确只搜单边，绝不因为双启用而默认全搜。
2. **选择发现入口**：
   - 已知关卡、密录、干员资料、活动全集名：直接 `prts_read`。
   - 已知原句、专名、短片段：以最确定的短字面量调用 `prts_search`。
   - 年代与年份顺序（方舟）：优先 `prts_timeline`。
   - 官方外语对照：直接调用 `prts_i18n`。
3. **检查首轮结果**：检查资料所属游戏、资源类型、是否存在截断。分数不是事实可信度，必须验证内容本身。
4. **收窄并核验**：
   - 需要精准台词/动作/说话人时，利用搜索返回的标题与行号，调用 `prts_read` 回读连续上下文。
   - 官方档案已明示且无冲突时即可停止，不强制再搜剧情。
5. **失败处理**：零命中时依次尝试“缩短连续字面串”、“核对规范名称”、“去除一个冲突过滤器”；禁止盲目重复同一调用。
6. **停止条件**：证据已完全覆盖用户问题且无冲突时停止，禁止为了形式完整盲目调用全套工具。

---

## 四、专属考据子技能导航

遇到具体领域的深度考据或遇到异常时，请直接查阅以下专属 Skill：
- **《明日方舟》本篇、活动、干员档案与密录考据**：查阅 `prts-arknights`。
- **《终末地》任务合集、内容形式与官方多语言反查**：查阅 `prts-endfield`。
- **Wiki 规范页结构、标签字段表与单字段免搜索直读**：查阅 `prts-wiki-schema`。
- **双游戏对比、再旅者（retraveler_relations）审校关系**：查阅 `prts-dual-retrieval`。
- **检索报错、零命中、document_uid 同名消歧与超时排查**：查阅 `prts-diagnostics`。
