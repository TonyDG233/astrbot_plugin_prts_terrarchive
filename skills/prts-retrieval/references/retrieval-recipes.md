# 明日方舟检索配方

以下流程只在明日方舟模块启用时使用，并始终带 `games:["arknights"]`。

## 角色与活动参与关系

1. 角色→活动：省略 `query`，调用 `prts_search({games:["arknights"], resource_types:["character_activity_wiki"], character_names:[角色]})`，从结果的 `activity_name` 整理活动。
2. 活动→角色：省略 `query`，调用 `prts_search({games:["arknights"], resource_types:["character_activity_wiki"], activity_names:[活动]})`，从结果的 `character_name` 整理角色。
3. 角色×活动：省略 `query`，调用 `prts_search({games:["arknights"], resource_types:["character_activity_wiki"], character_names:[角色], activity_names:[活动]})`；不同过滤字段取精确交集，可用于确认这对关系并读取对应辅助页。
4. 完整清单或确认零命中时，保留原条件并原样提交 `page.next_after`，直到 `page.exhausted=true`，再按角色名或活动名去重。穷尽后仍无结果只表示当前资料版本没有该角色×活动整理记录；不得移除一个过滤条件后把任一侧的单独命中当作参与关系，也不能据此断言剧情中绝无参与。

## 某角色在某活动中的作用

1. 查 `story_wiki + activity_names + wiki_sections:["角色剧情概括"] + query:角色`。
2. 需要更细过程时，查 `character_activity_wiki + character_names + activity_names + wiki_sections:["相关剧情总结"]`。
3. 需要具体行动、说话人或因果时，以活动和角色为范围搜索 `story/operator_record`，再读取命中上下文。

## 某活动的整体研究

1. 若需现成概括，直接读 `story_wiki` 的 `剧情总结`。
2. 读 `关键人物` 建立人物集合，按需读取目标人物的 `角色剧情概括`。
3. 用 `prts_timeline(activity_names:[活动])` 补时间位置。
4. 只对会影响结论的关键事件回到官方原文；用户明确要求完整顺读时才用 `prts_read({activity_name:活动, mode:"activity"})`。

## 人物关系

1. 分别读取双方 `character_wiki` 的 `相关角色`，检查是否双向出现及叙述是否一致。
2. 对关系起因、变化、冲突或态度，按活动或剧情线索以 `prts_search` 检索 `story/operator_record` 原文并读取上下文。

## 台词与说话人

1. 确切短句：`prts_search({games:["arknights"], query:短句, resource_types:["story","operator_record"]})`。
2. 查询某人亲口说的话时加 `speakers:[角色]`；查询谈及某人时才用 `entity_names`。
3. 若只记得几个连续字词，可缩短 query 为最确定的连续字面串检索并读取上下文。

## 人物综合资料

先用角色 Wiki 的 `简要介绍/详细介绍/相关活动` 建立骨架；身份和身体资料查 `character_profile`，补充经历查 `character_module/operator_record`，原话与称呼查 `character_voice/story`。只核验实际用于回答的结论。
