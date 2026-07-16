from __future__ import annotations

import re


PLUGIN_NAME = "astrbot_plugin_who_at_me_pro"
LEGACY_PLUGIN_NAME = "astrbot_plugin_who_at_me"
MAX_IMAGE_SOURCE_BYTES = 20 * 1024 * 1024
MEMBER_CACHE_TTL_SECONDS = 5 * 60


QUERY_PATTERN = re.compile(r"^(璋?鑹剧壒|@|at)(鎴憒浠東濂箌瀹?|鍝釜閫?鑹剧壒|@|at)鎴?(?:\s*(?:\[CQ:at,[^\]]+\]|@.+))?$", re.I)
CLEAR_PATTERN = re.compile(r"^(clear_at|娓呴櫎(鑹剧壒|at)鏁版嵁)$", re.I)
CLEAR_ALL_PATTERN = re.compile(r"^(clear_all|娓呴櫎鍏ㄩ儴(鑹剧壒|at)鏁版嵁)$", re.I)
CONTEXT_ON_PATTERN = re.compile(r"^(寮€鍚瘄鎵撳紑)(鑹剧壒|at)涓婁笅鏂?", re.I)
CONTEXT_OFF_PATTERN = re.compile(r"^鍏抽棴(鑹剧壒|at)涓婁笅鏂?", re.I)
REMINDER_GROUP_ON_PATTERN = re.compile(r"^(寮€鍚瘄鍚敤)(鏈兢|缇?(鑹剧壒|at)鎻愰啋$", re.I)
REMINDER_GROUP_OFF_PATTERN = re.compile(r"^鍏抽棴(鏈兢|缇?(鑹剧壒|at)鎻愰啋$", re.I)
REMINDER_PERSONAL_ON_PATTERN = re.compile(r"^(寮€鍚垜鐨?鑹剧壒|at)鎻愰啋|寮€鍚?鑹剧壒|at)鎻愰啋)$", re.I)
REMINDER_PERSONAL_OFF_PATTERN = re.compile(r"^(鍏抽棴鎴戠殑(鑹剧壒|at)鎻愰啋|鍏抽棴(鑹剧壒|at)鎻愰啋)$", re.I)
REMINDER_STATUS_PATTERN = re.compile(r"^(鎴戠殑)?(鑹剧壒|at)鎻愰啋鐘舵€?", re.I)
REMINDER_CONTEXT_ON_PATTERN = re.compile(r"^寮€鍚彁閱掍笂涓嬫枃$", re.I)
REMINDER_CONTEXT_OFF_PATTERN = re.compile(r"^鍏抽棴鎻愰啋涓婁笅鏂?", re.I)
REMINDER_CONTEXT_SET_PATTERN = re.compile(r"^璁剧疆鎻愰啋涓婁笅鏂嘰s*(\d+)\s*[,锛宂\s*(\d+)$", re.I)

ALL_TARGET = "__all__"
INDEX_KEY = "records:index"
CONTEXT_INDEX_KEY = "context:index"
REMINDER_PENDING_INDEX_KEY = "reminder:pending:index"
MAX_RECORDS_PER_TARGET = 300
RECENT_IMAGE_CACHE_RECORDS = 0
IMAGE_CACHE_RETENTION_HOURS = 24
MAX_CONTEXT_MESSAGES = 5
MAX_MESSAGES_PER_IMAGE = 12
RENDER_IMAGE_QUALITY = 92
RENDER_TIMEOUT_MS = 20000
RENDER_TASK_TIMEOUT_SEC = 25
REMINDER_AWAY_SECONDS = 10 * 60
MAX_PENDING_REMINDERS = 50
MAX_REMINDER_CONTEXT = 5
LEGACY_HEADER_IMAGE_URL = "https://pic1.imgdb.cn/item/69e60edc1d6508f56becb8fa.png"
LEGACY_FOOTER_IMAGE_URL = "https://pic1.imgdb.cn/item/69e5f9e51d6508f56bec8ea5.png"
HEADER_IMAGE_URL = ""
FOOTER_IMAGE_URL = ""
DEFAULT_HEADER_IMAGE_FILE = "assets/default_header.png"
DEFAULT_FOOTER_IMAGE_FILE = "assets/default_footer.png"
IMAGE_KINDS = {"header": "椤堕儴鍥剧墖", "footer": "搴曢儴鍥剧墖"}
IMAGE_MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
REFERENCE_SEGMENT_TYPES = {"reply", "quote", "source", "reference"}
POKE_SEGMENT_TYPES = {"poke", "nudge", "touch", "pat"}
POKE_ACTION_TOKENS = ("鎴充簡鎴?, "鎷嶄簡鎷?, "鎽镐簡鎽?, "鎻変簡鎻?, "浜蹭簡浜?, "璐翠簡璐?, "纰颁簡纰?)
PAGE_SETTINGS_DEFAULTS = {
    "time_x": 30,
    "time_y": 7,
    "time_font_size": 16,
    "group_x": 56,
    "group_y": 45,
    "group_font_size": 22,
    "font_bold": False,
    "font_bold_strength": 0,
    "font_path": "",
    "header_image_path": "",
    "footer_image_path": "",
}
