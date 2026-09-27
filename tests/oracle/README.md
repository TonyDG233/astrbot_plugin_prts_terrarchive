# 黄金对照（dev-only）

用上游 Node 实现渲染同一份合成语料，人工核对我们移植的模型可见文本。
**不属于 pytest**（依赖本机 Node ≥20 与上游仓库源码）。

## 前置

- Node ≥ 20 已安装
- 上游仓库存在（默认 `D:/LLM/self_programming/prts-terrarchive`，可用环境变量 `PRTS_UPSTREAM` 覆盖）
- 先生成一份合成语料（会落在给定目录）：

```powershell
python -c "import sys; sys.path.insert(0, r'.'); sys.path.insert(0, r'tests'); import fixtures; print(fixtures.make_full_fixture(r'$env:TEMP\prts-oracle-fixture')['releases_dir'])"
```

## 运行

```powershell
node tests\oracle\run_oracle.mjs --release "$env:TEMP\prts-oracle-fixture" --query "凯尔希"
node tests\oracle\run_oracle.mjs --release "$env:TEMP\prts-oracle-fixture" --stage CW-ST-4 --story-part after
node tests\oracle\run_oracle.mjs --release "$env:TEMP\prts-oracle-fixture" --query "凯尔希" --json
```

## 与 Python 对照

`renderSearch` 上游返回单个 `{type:'text', text}` 分片，本仓库 `render_search` 返回该 `text` 本身；
逐行 diff 即可。已核对为逐字节一致（合成语料、2026-09）：

| 场景 | 结果 |
|---|---|
| `search-凯尔希` | 68 行 0 diff |
| `search-源石` | 60 行 0 diff |
| `search-特里蒙` | 46 行 0 diff |
| `search-管理员` | 12 行 0 diff |
| `search-罗德岛` | 13 行 0 diff |
| `read-CW-ST-4`（document 全篇） | 45 行 0 diff |

## 注意

- 上游契约只接受 `document_id`/`display_title` 等 locator；`--stage` 分支先在 store 里
  解析为 `document_id` 再读，与 Python 侧 `model_read_to_contract` 的解析语义对齐。
- 上游 `projectSearch` 用 `\n\n` 连接文档块；空行数量以 `node -e` 打印的 `JSON.stringify`
  为准（控制台重定向曾观察到伪影）。
- 若上游渲染中出现 `[corpus_search:error]` 前缀而 Python 为 `[prts_search:error]`，
  属于有意差异（工具更名）。