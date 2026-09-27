#!/usr/bin/env node
/**
 * 开发期黄金对照工具（不属于 pytest）。
 *
 * 用上游 prts-terrarchive 的 JS 实现渲染同一份合成语料，供人工比对 Python 移植的
 * 模型可见输出。需要 Node >= 20，且本机存在上游仓库源码。
 *
 * 用法：
 *   node tests/oracle/run_oracle.mjs --release <releases目录（含 current.json）> --query "凯尔希"
 *   node tests/oracle/run_oracle.mjs --release <dir> --stage CW-ST-4 --story-part after
 *   node tests/oracle/run_oracle.mjs --release <dir> --query "凯尔希" --json
 *
 * 环境变量：
 *   PRTS_UPSTREAM  上游仓库路径（默认 D:/LLM/self_programming/prts-terrarchive）
 */
import fs from 'node:fs'
import path from 'node:path'
import process from 'node:process'
import { pathToFileURL } from 'node:url'

const upstream = process.env.PRTS_UPSTREAM || 'D:/LLM/self_programming/prts-terrarchive'

const parsed = {}
const argv = process.argv.slice(2)
for (let index = 0; index < argv.length; index += 1) {
  if (argv[index].startsWith('--')) {
    const key = argv[index].slice(2)
    const next = argv[index + 1]
    parsed[key] = next && !next.startsWith('--') ? (index += 1, next) : true
  }
}

let releaseDir = typeof parsed.release === 'string' ? parsed.release : ''
if (!releaseDir && argv[0] && !argv[0].startsWith('--')) {
  releaseDir = argv[0]
}
if (!releaseDir || !fs.existsSync(path.join(releaseDir, 'current.json'))) {
  console.error('用法: node run_oracle.mjs <releases目录 | --release <releases目录>> [--query 文本 | --stage 关卡代号] [--story-part before|after|story] [--json]')
  process.exit(2)
}
if (!fs.existsSync(path.join(upstream, 'src', 'store.js'))) {
  console.error(`找不到上游源码：${upstream}（可用 PRTS_UPSTREAM 指定）`)
  process.exit(2)
}

const load = (file) => import(pathToFileURL(path.join(upstream, 'src', file)).href)
const { CorpusStore } = await load('store.js')
const { executeSearch, renderSearch } = await load('search.js')
const { executeRead, projectReadPublic, renderRead } = await load('read.js')

const store = new CorpusStore({ releasesDir: releaseDir })
await store.ready()

let output
const flatten = (value) =>
  Array.isArray(value) ? value.map((part) => (part && part.text) || '').join('\n') : value
if (typeof parsed.query === 'string' || parsed.query === true || (!parsed.stage)) {
  const queryText = typeof parsed.query === 'string' ? parsed.query : '风沙掠过'
  const args = { query: queryText }
  const value = await executeSearch(store, args, {})
  output = parsed.json ? JSON.stringify(value, null, 2) : flatten(renderSearch(args, value))
} else if (typeof parsed.stage === 'string') {
  const storyPart = typeof parsed['story-part'] === 'string' ? parsed['story-part'] : ''
  const found = await store.getDocumentByStoryStage(parsed.stage, storyPart || undefined)
  if (!found) {
    console.error(`找不到关卡 ${parsed.stage}${storyPart ? `/${storyPart}` : ''}`)
    process.exit(3)
  }
  const contract = {
    intent_id: 'oracle-run',
    locator: { document_id: found.record.document.document_id },
    selection: { mode: 'document' },
  }
  const value = await executeRead(store, contract, {})
  output = parsed.json ? JSON.stringify(value, null, 2) : flatten(renderRead({}, projectReadPublic(value)))
} else {
  console.error('需要 --query 或 --stage')
  process.exit(2)
}

console.log(output)
console.error(`\n[oracle] upstream=${upstream}`)