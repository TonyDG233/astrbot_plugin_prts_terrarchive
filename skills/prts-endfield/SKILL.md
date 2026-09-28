---
name: prts-endfield
description: 《明日方舟：终末地》任务合集、官方剧情原文、档案与知识设定资料检索，content_types 内容形式过滤，以及基于 prts_i18n 的多语言（EN/JP/KR）官方文本反查配方。
---

# 终末地资料检索与多语言对照配方

本技能适用于所有《明日方舟：终末地》世界观的问题。调用工具时必须始终显式指定 `games: ["endfield"]`（除非问题明确需要与《明日方舟》本篇联合考据）。

---

## 一、终末地本地资料类型全景

终末地采用跨游戏统一架构，**严禁套用明日方舟干员资料类型**（如 `operator_record`、`character_profile` 在终末地不存在）。

| 资源类型 `resource_types` | 说明与适用场景 |
| :--- | :--- |
| `original_story` | **官方剧情原文**。包含全部主线、任务剧情与碎片。必须善用 `content_types`。 |
| `archive` | **官方档案**。最权威的官方设定、机构档案、人物背景记录。 |
| `knowledge` | **知识与世界观资料**。技术、地理、生态、专有名词定义。 |
| `character_story` | **角色故事**。角色的个人叙事与独立背景故事。 |
| `timeline` | **时间线资料**。终末地事件整理线索（通过 `prts_search` 查询，**严禁用 `prts_timeline`**）。 |
| `wiki` | **整理性 Wiki**。第三方概括与词条总结。 |
| `entity_profile` | **实体概述**。实体基本定义与全套关联资料的索引入口。 |

### 关键：`content_types` 9 大内容形式过滤
终末地原文资料类型极其丰富，在检索 `original_story` 时，添加 `content_types` 能彻底排除无关形式的干扰：
- `dialogue`：角色面对面对话。
- `cutscene`：过场动画或 CG 场景文本。
- `radio`：无线电广播。
- `remote_comm`：远程通信对话。
- `black_screen`：黑屏文字与转场内心独白。
- `environment_talk`：环境 NPC 闲聊与背景窃窃私语。
- `sns_topic`：社交媒体/终端公开话题。
- `sns_chat`：社交媒体私聊或小群讨论。
- `narration`：旁白描述与叙述。

---

## 二、定位、合集通读与排序规则

1. **定位字段优先级**：
   - 任务或资料集合：优先使用 `collection_names`（如 `collection_names: ["武陵特厨"]`）。
   - 角色归属：使用 `character_names`；台词亲口说话人使用 `speakers`；正文提及使用 `entity_names`。
2. **整任务连续阅读**：
   ```json
   prts_read({
     "collection_name": "任务展示全名",
     "mode": "collection"
   })
   ```
   可附加 `content_types: ["dialogue", "remote_comm"]` 只读对话部分。
3. **碎片排序铁律**：
   - 当终末地剧情碎片缺乏游戏内可证明的全局时间线时，工具仅按内容类型分组并在组内按自然编号呈现，结果带有 `ordering_note` 明确说明。
   - **严禁将工具返回的碎片前后顺序主观表述为剧情时间发生的先后顺序**！
4. **同名多合集消歧**：
   - 若直接传入 `collection_name` 报错存在同名多个合集，先用 `prts_search({games: ["endfield"], collection_names: ["任务名"], resource_types: ["original_story"]})` 检索出单篇；
   - 提取结果给出的 `document_uid`，再以 `prts_read({document_uid: "uid", mode: "collection"})` 选定特定合集通读。

---

## 三、`prts_i18n` 官方多语言对照反查规范

用于查询终末地官方外语文本对照，**严禁使用非官方大模型机翻冒充官方文本**。

1. **标准调用方式**：
   - **已有原句或专名（反查外语）**：
     ```json
     prts_i18n({
       "query": "佩丽卡",
       "languages": ["EN", "JP"]
     })
     ```
   - **已有剧情标题与行号（精准定位外语对照）**：
     ```json
     prts_i18n({
       "title": "任务第一章·前瞻",
       "line": 15,
       "languages": ["EN"]
     })
     ```
   - **外语反查中文**：指定 `source_language: "EN"`，输入外文短语查询对应的官方中文文本。
2. **多语言结果解读与引用准则**：
   - `alignment: "record"` 表示整条角色档案对照；`alignment: "document"` 表示资料块对照；**各语言间并不保证逐句一一对应**。
   - 长文本按字符分块，`character_start/end` 为该语言独立的位置指标；续读时原样提交返回的 `page.continuation`。
   - 引用时注明工具返回的具体篇章、语言和中文原定位，不得将中文第 N 行直接换算为英文第 N 行。
   - 若返回 `missing_localization`（官方未出外语版）或 `unmapped_source`，直接说明官方尚未包含该语言，不得自行捏造译名。

---

## 四、终末地六大典型场景解决配方

### 配方 1：任务流程与主线剧情考据
1. 已知完整任务名且需顺读剧情：直接调用 `prts_read({collection_name: 任务名, mode: "collection"})`。
2. 遇到信息过杂时，传入 `content_types: ["dialogue", "cutscene"]` 过滤掉环境闲聊与 SNS。
3. 发现关键线索行后，以该行行号为中心使用 `prts_read({title: 篇章名, line: 行号, before: 3, after: 3})` 扩大上下文核验前因后果。

---

### 配方 2：台词、广播或通讯核实
1. **已知短语检索**：
   ```json
   prts_search({
     "games": ["endfield"],
     "resource_types": ["original_story"],
     "query": "关键台词字面",
     "content_types": ["dialogue", "remote_comm", "radio"]
   })
   ```
2. **限定某人亲口说的话**：必须加 `speakers: ["角色名"]`。
3. **扩大核对**：获得候选行后，必须回读该篇原文行上下文，确认发言者语境、是否有通讯杂音或代号代指。

---

### 配方 3：人物综合档案与过往背景
1. **第一步（全局入口）**：查询实体概述，建立人物背景框架：
   ```json
   prts_search({
     "games": ["endfield"],
     "resource_types": ["entity_profile"],
     "query": "角色名"
   })
   ```
2. **第二步（官方档案）**：查询最权威的官方人事档案：
   ```json
   prts_search({
     "games": ["endfield"],
     "resource_types": ["archive"],
     "character_names": ["角色名"]
   })
   ```
3. **第三步（个人故事与表现）**：查询 `character_story` 了解独立经历；查询 `original_story` 了解实际剧情中的行动表现。
4. **第四步（官方外文名称核实）**：调用 `prts_i18n` 获取其 EN、JP 官方罗马音/日文译名。

---

### 配方 4：设定、地点或组织定义考证
1. 涉及正式机构、技术装置或地理名称时，先查权威官方档案：
   ```json
   prts_search({
     "games": ["endfield"],
     "resource_types": ["archive", "knowledge"],
     "query": "组织或设定名词"
   })
   ```
2. 若需了解该设定在剧情中是如何被实际提及与展示的，再以 `original_story` 搜索相关对话；
3. 回答时明确区分“官方档案中的标准定义”与“剧情人物口中的描述”。

---

### 配方 5：终末地时间与事件顺序考据
1. 终末地时间线只能通过 `prts_search` 检索：
   ```json
   prts_search({
     "games": ["endfield"],
     "resource_types": ["timeline"],
     "query": "事件名词或实体名"
   })
   ```
2. **绝对禁令**：严禁调用明日方舟专属的 `prts_timeline` 工具来推断终末地无此事件！
3. 时间线属于整理性证据；具体事件过程必须回读 `original_story` 原文。

---

### 配方 6：某类资料目录快速浏览
- 省略 `query`，传入 `collection_names`（或 `character_names`）配合目标 `resource_types`（如 `["archive"]`）：
  ```json
  prts_search({
    "games": ["endfield"],
    "resource_types": ["archive"],
    "collection_names": ["目标集合"]
  })
  ```
  直接输出该集合下的全部官方档案清单，避免被正文同词提及淹没。
