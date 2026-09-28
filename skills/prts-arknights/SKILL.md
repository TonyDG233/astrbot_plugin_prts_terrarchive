---
name: prts-arknights
description: 《明日方舟》本篇剧情、活动剧情、干员密录、干员档案/模组/语音/时装资料及泰拉年表的精准检索与六大考据场景（角色活动参与、活动作用、人物关系、原句台词、综合档案）解决配方。
---

# 明日方舟资料检索与考据配方

本技能适用于所有《明日方舟》本篇世界观的问题。调用工具时必须始终显式指定 `games: ["arknights"]`（除非问题明确需要与《终末地》联合考据）。

---

## 一、明日方舟本地资料类型全景

| 分类 | `resource_types` 可选值 | 说明与最佳适用场景 |
| :--- | :--- | :--- |
| **剧情原文** | `story` | 主线章节、故事集、SideStory 的官方剧情原文。支持台词、动作、场景与因果。 |
| **密录原文** | `operator_record` | 干员密录原文。记录干员个人专属独立故事。 |
| **官方角色资料** | `character_profile` | 干员档案（基础信息、体检、履历、档案 1~4、晋升记录、信物描述）。 |
| ^ | `character_module` | 干员专属模组背景故事与设定文本。 |
| ^ | `character_voice` | 干员语音台词、对话情境与称呼。 |
| ^ | `character_skin` | 时装立绘伴随的官方背景与情境描写。 |
| ^ | `character_bundle` | 官方角色资料全集（不确定信息在档案、模组还是语音时一次性覆盖）。 |
| **Wiki 整理资料** | `character_wiki` | 规范角色页。身份、生平概述、人物关系表、高光、表现。 |
| ^ | `story_wiki` | 活动/主线/密录页。剧情总结、关键人物表、角色剧情概括、轶事。 |
| ^ | `character_activity_wiki` | 角色×活动交叉辅助页。特定角色在特定活动中的剧情总结与表现。 |
| ^ | `reviewed_wiki` | 跨所有 Wiki 宽搜（仅在上述精准类型无法定位时兜底使用）。 |
| **设定与年表** | `terra_journey` | 《大地巡旅》官方世界观设定文本。 |
| ^ | `entity_profile` | 实体词条概述与关系导航。 |
| ^ | `reference` | 综合参考资料、术语对照表。 |

---

## 二、定位与过滤核心规则

1. **角色字段三要素区分**：
   - `character_names`：**资料归属**（如该档案属于谁、该 Wiki 页面介绍谁）。
   - `speakers`：**台词亲口说话人**（查“某人亲口说的话”必须使用 `speakers`，切勿用 `entity_names`）。
   - `entity_names`：**正文提及实体**（正文台词或叙述中出现了某人、某势力或某名词）。
   - 上述字段在同一个请求中是 **AND（交集）** 关系。
2. **结构定位器**：
   - 活动使用 `activity_names: ["孤星"]`；
   - 单篇章节使用 `story_names: ["15-17"]`；
   - 上级集合也可使用 `collection_names`。
3. **优先直读路径（满足条件直接调用 `prts_read`，无需先 `prts_search`）**：
   - **关卡代号**：`prts_read({stage_code: "15-17"})`；若报存在多篇，补充 `story_part: "before"|"after"|"story"`。
   - **干员密录**：`prts_read({character_name: "凯尔希", record_name: "遗老"})`；多段密录加整数 `segment: 2`。
   - **干员官方资料**：`prts_read({character_name: "凯尔希", material: "profile"})`（`material` 支持 `profile`、`module`、`voice`、`skin`、`recruitment`、`potential`）。
   - **整活动连续通读**：`prts_read({activity_name: "孤星", mode: "activity"})`；若报同名歧义，先搜单篇取得 `document_uid`，再用 `{document_uid, mode: "activity"}`。

---

## 三、六大典型考据场景解决配方

### 配方 1：角色与活动的参与关系
- **角色→参加过哪些活动**：
  ```json
  prts_search({
    "games": ["arknights"],
    "resource_types": ["character_activity_wiki"],
    "character_names": ["凯尔希"]
  })
  ```
  从返回结果的 `activity_name` 整理出活动清单。
- **活动→有哪些角色登场**：
  ```json
  prts_search({
    "games": ["arknights"],
    "resource_types": ["character_activity_wiki"],
    "activity_names": ["孤星"]
  })
  ```
  从返回结果的 `character_name` 整理出人物列表。
- **验证某角色是否参与某活动**：
  ```json
  prts_search({
    "games": ["arknights"],
    "resource_types": ["character_activity_wiki"],
    "character_names": ["凯尔希"],
    "activity_names": ["孤星"]
  })
  ```
- **穷尽断言规则**：
  - 需要完整清单或断言未参加时，必须原样提交 `page.next_after` 翻页直至 `page.exhausted=true`。
  - 穷尽后仍为 0 命中，只表示“当前资料版本没有收录该角色在该活动的整理记录”；不得据此绝对断言剧情中从未提及，也不能去掉过滤条件拿单侧命中冒充。

---

### 配方 2：某角色在特定活动中的具体表现与作用
1. **第一步（Wiki 概括）**：
   ```json
   prts_search({
     "games": ["arknights"],
     "resource_types": ["story_wiki"],
     "activity_names": ["目标活动名"],
     "wiki_sections": ["角色剧情概括"],
     "query": "角色名"
   })
   ```
2. **第二步（辅助页细化）**：若需更完整的行动线，查询交叉辅助页：
   ```json
   prts_search({
     "games": ["arknights"],
     "resource_types": ["character_activity_wiki"],
     "character_names": ["角色名"],
     "activity_names": ["活动名"],
     "wiki_sections": ["相关剧情总结"]
   })
   ```
3. **第三步（原文核实）**：涉及关键台词、生死因果或具体动机时，拿上述命中给出的篇章和行号，用 `prts_read` 回读 `story` 原文连续上下文。

---

### 配方 3：某活动的整体剧情与脉络研究
1. **剧情主线与结局速查**：查询 `story_wiki` 的 `剧情总结` 字段：
   ```json
   prts_search({
     "games": ["arknights"],
     "resource_types": ["story_wiki"],
     "activity_names": ["活动名"],
     "wiki_sections": ["剧情总结"]
   })
   ```
2. **核心出场人物结构**：查询该活动的 `关键人物` 字段。
3. **时间线定位**：
   ```json
   prts_timeline({
     "activity_names": ["活动名"]
   })
   ```
4. **原文校验**：对影响整体结论的关键高光转折，回读 `story` 原文；只有用户明确要求从头到尾通读时，才使用 `prts_read({activity_name: "活动名", mode: "activity"})`。

---

### 配方 4：干员之间的人物关系与态度考据
1. **双向 Wiki 关系表核对**：
   - 查 A 的角色 Wiki：`prts_search({games: ["arknights"], resource_types: ["character_wiki"], character_names: ["A"], wiki_sections: ["相关角色"]})`；
   - 查 B 的角色 Wiki：`prts_search({games: ["arknights"], resource_types: ["character_wiki"], character_names: ["B"], wiki_sections: ["相关角色"]})`；
   - 检查双方关系描述是否对称、是否有矛盾。
2. **关系起因与冲突过程核验**：
   - 根据关系描述中提及的事件或篇章，用 `prts_search({games: ["arknights"], resource_types: ["story", "operator_record"], entity_names: ["对方名"], character_names: ["主角名"]})` 定位原文；
   - 调用 `prts_read` 核验原句台词与情感语境。

---

### 配方 5：精确原句台词与说话人验证
1. **已知确定原句（4 字以上确定字面）**：
   ```json
   prts_search({
     "games": ["arknights"],
     "resource_types": ["story", "operator_record"],
     "query": "确定台词片段"
   })
   ```
2. **限定某人亲口说的话**：必须加 `speakers: ["说话人"]`。
3. **记忆模糊时**：
   - 缩短 `query` 为最确定连续字面词（如 2~3 字）；
   - 加上所属章节或活动名（`collection_names` 或 `activity_names`）；
   - 搜索命中后，必须使用 `prts_read` 读取该行前后至少 3 行上下文（`before: 3, after: 3`），核对发言者究竟是谁、是否包含反语或代指。

---

### 配方 6：干员综合生平与能力背景
1. **生平骨架**：读取 `character_wiki` 的 `简要介绍` 与 `详细介绍` 字段。
2. **生理指标与官方背景**：读取 `character_profile`（`prts_read({character_name: "干员名", material: "profile"})`）。
3. **隐藏背景与过往经历**：读取 `character_module`（模组文本）或 `operator_record`（干员密录）。
4. **口吻、称呼与情感倾向**：读取 `character_voice`（语音台词）。
5. 最终结论区分“官方档案明示”（profile/module）与“Wiki 人工提炼概括”。
