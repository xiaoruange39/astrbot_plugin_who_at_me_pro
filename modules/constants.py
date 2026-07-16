from __future__ import annotations

import re


PLUGIN_NAME = "astrbot_plugin_who_at_me_pro"
LEGACY_PLUGIN_NAME = "astrbot_plugin_who_at_me"
MAX_IMAGE_SOURCE_BYTES = 20 * 1024 * 1024
MEMBER_CACHE_TTL_SECONDS = 5 * 60


QUERY_PATTERN = re.compile(r"^(谁(艾特|@|at)(我|他|她|它)|哪个逼(艾特|@|at)我)(?:\s*(?:\[CQ:at,[^\]]+\]|@.+))?$", re.I)
CLEAR_PATTERN = re.compile(r"^(clear_at|清除(艾特|at)数据)$", re.I)
CLEAR_ALL_PATTERN = re.compile(r"^(clear_all|清除全部(艾特|at)数据)$", re.I)
CONTEXT_ON_PATTERN = re.compile(r"^(开启|打开)(艾特|at)上下文$", re.I)
CONTEXT_OFF_PATTERN = re.compile(r"^关闭(艾特|at)上下文$", re.I)
REMINDER_GROUP_ON_PATTERN = re.compile(r"^(开启|启用)(本群|群)(艾特|at)提醒$", re.I)
REMINDER_GROUP_OFF_PATTERN = re.compile(r"^关闭(本群|群)(艾特|at)提醒$", re.I)
REMINDER_PERSONAL_ON_PATTERN = re.compile(r"^(开启我的(艾特|at)提醒|开启(艾特|at)提醒)$", re.I)
REMINDER_PERSONAL_OFF_PATTERN = re.compile(r"^(关闭我的(艾特|at)提醒|关闭(艾特|at)提醒)$", re.I)
REMINDER_STATUS_PATTERN = re.compile(r"^(我的)?(艾特|at)提醒状态$", re.I)
REMINDER_CONTEXT_ON_PATTERN = re.compile(r"^开启提醒上下文$", re.I)
REMINDER_CONTEXT_OFF_PATTERN = re.compile(r"^关闭提醒上下文$", re.I)
REMINDER_CONTEXT_SET_PATTERN = re.compile(r"^设置提醒上下文\s*(\d+)\s*[,，]\s*(\d+)$", re.I)

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
IMAGE_KINDS = {"header": "顶部图片", "footer": "底部图片"}
IMAGE_MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
REFERENCE_SEGMENT_TYPES = {"reply", "quote", "source", "reference"}
POKE_SEGMENT_TYPES = {"poke", "nudge", "touch", "pat"}
POKE_ACTION_TOKENS = ("戳了戳", "拍了拍", "摸了摸", "揉了揉", "亲了亲", "贴了贴", "碰了碰")
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
