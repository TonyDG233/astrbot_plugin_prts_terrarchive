"""跨模块共享常量：契约版本、资源/内容类型、Wiki 字段、限额与路径/哈希正则。

数值与上游一致；修改前先对照 `prts-terrarchive/src/{store,search,read,installer,wiki}.js`。
"""

import re

# 插件自身版本（对应上游 release-compatibility.js 的 AGENT_VERSION，用于
# release-manifest.minimum_agent_version 兼容检查）。与上游 package.json 0.2.0 对齐：
# 含 localization 附件的 release 要求 minimum_agent_version >= 0.2.0。
AGENT_VERSION = "0.2.5"

# 契约版本（read.js:20）。
CONTRACT_VERSION = "prts-corpus-tools-v1"

# AstrBot 工具名（全局命名空间，统一前缀 prts_）。
TOOL_SEARCH = "prts_search"
TOOL_READ = "prts_read"
TOOL_TIMELINE = "prts_timeline"
TOOL_I18N = "prts_i18n"

# index.js RESOURCE_TYPES。
RESOURCE_TYPES = (
    "story",
    "character_profile",
    "character_module",
    "character_voice",
    "character_skin",
    "operator_record",
    "character_bundle",
    "character_wiki",
    "story_wiki",
    "character_activity_wiki",
    "reviewed_wiki",
    "terra_journey",
    "entity_profile",
    "reference",
    "original_story",
    "archive",
    "knowledge",
    "wiki",
    "character_story",
    "timeline",
)

# index.js CONTENT_TYPES。
CONTENT_TYPES = (
    "dialogue",
    "cutscene",
    "radio",
    "remote_comm",
    "black_screen",
    "environment_talk",
    "sns_topic",
    "sns_chat",
    "narration",
    "archive",
    "knowledge",
)

# read.js END_FIELD_STORY_CONTENT_TYPES（终末地合集过滤）。
END_FIELD_STORY_CONTENT_TYPES = (
    "dialogue",
    "cutscene",
    "radio",
    "remote_comm",
    "black_screen",
    "environment_talk",
    "sns_topic",
    "sns_chat",
    "narration",
)

# wiki.js WIKI_SECTION_VALUES。
WIKI_SECTION_VALUES = (
    "简要介绍",
    "相关角色",
    "详细介绍",
    "剧情高光",
    "战斗表现",
    "相关活动",
    "trivia",
    "角色点评",
    "剧情总结",
    "关键人物",
    "角色剧情概括",
    "所有相关的活动剧情总结",
    "相关剧情总结",
    "相关剧情高光",
    "相关trivia",
    "相关角色总结",
)

# installer.js PACK_IDS。
PACK_IDS = (
    "official_game",
    "endfield_official_game",
    "endfield_reviewed_knowledge",
    "reviewed_wiki",
    "terra_journey",
    "entities",
    "references",
)

REQUIRED_GAME_PACK = {"arknights": "official_game", "endfield": "endfield_official_game"}

# installer.js CORPUS_RESOURCE_LIMITS（本地解析器的安全不变量，不随配置放宽）。
CORPUS_RESOURCE_LIMITS = {
    "maxAssets": 4096,
    "maxAssetCompressedBytes": 64 * 1024 * 1024,
    "maxAssetUncompressedBytes": 128 * 1024 * 1024,
    "maxReleaseCompressedBytes": 1024 * 1024 * 1024,
    "maxReleaseUncompressedBytes": 4 * 1024 * 1024 * 1024,
}

# 清单请求的显式超时与响应体大小上限（installer.js:106-110）。
MANIFEST_TIMEOUT_SECONDS = 20
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_CURRENT_POINTER_BYTES = 64 * 1024
MAX_UNVERIFIED_BYTES = 4 * 1024 * 1024

# installer.js:111-119。
RELEASE_ALGORITHM = "prts-browser-corpus-release-v1"
PACK_ALGORITHMS = {
    "prts-browser-corpus-pack-v1": 1,
    "prts-browser-corpus-pack-v2": 2,
}
SUPPORTED_SEARCH_INDEX_ALGORITHMS = {
    "prts-browser-trigram-postings-v1": 1,
    "prts-browser-ngram-postings-v2": 2,
}

# 上游 installer.js:89-90、read.js:22-25。
RELEASE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
DATA_VERSION_PATTERN = re.compile(r"^[0-9a-f]{64}$")
ASSET_PATH_PATTERN = re.compile(
    r"^(?:shards/[A-Za-z0-9._-]+\.jsonl|search-index/[A-Za-z0-9._-]+\.bin"
    r"|(?:catalog|localization)/[A-Za-z0-9._-]+\.jsonl)\.gz$"
)
SOURCE_REF_PATTERN = re.compile(
    r"^(?:(?:official_game:(?:story:[^:]+|character:[^:]+:[^:]+)"
    r"|client_data:(?:reviewed_wiki|terra_journey|entities|references):[0-9a-f]{24})"
    r"|prts:(?:arknights|endfield):[A-Za-z0-9._:%/-]+):L([1-9][0-9]*)$"
)

# 数据来源站点（可信元数据固定 origin）。
DEFAULT_SITE_BASE_URL = "https://prts.chat"
MODELSCOPE_REPOS = {
    "arknights": "HTiantian/prts-agent-corpus-arknights",
    "endfield": "HTiantian/prts-agent-corpus-endfield",
    "official": "HTiantian/prts-agent-corpus-arknights-gamedata",
    "community": "HTiantian/prts-agent-corpus-selfbuilt",
}
DEFAULT_RELEASE_ID = "agent-corpus-v2-20260903-xuesong-youmeng-v1"

# 工具内预算（read.js/index.js）。
DEFAULT_READ_MAX_LINES = 100
DEFAULT_READ_MAX_CHARS = 12000
MAX_READ_MAX_LINES = 500
MAX_READ_MAX_CHARS = 100000

# 模型可见文本预算：AstrBot 在 ~27.5k tokens 处落盘截断，工具自带保守上限。
MAX_TOOL_OUTPUT_CHARS = 60000

LANGUAGE_CODES = ("CN", "EN", "JP", "KR", "TC", "MX", "BR", "FR", "DE", "RU", "IT", "ID", "TH", "VN")

LOCAL_CORPUS_MISSING_MESSAGE = (
    "本地数据包暂未安装，请提醒用户前往 AstrBot 的 PRTS 泰拉档案设置中下载，"
    "或由管理员执行 /prts 更新。"
)