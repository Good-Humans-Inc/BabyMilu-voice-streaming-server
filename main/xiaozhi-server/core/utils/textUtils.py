import json
import os
import re

TAG = __name__

def _load_emoji_mapping():
    """Load emoji -> (emotion, canonical_emoji) from emoji_mapping_raw.txt
    Format: emotion = emoji1emoji2emoji3 (no spaces or special chars needed)
    """
    path = os.path.join(os.path.dirname(__file__), "..", "..", "emoji_mapping_raw.txt")
    m = {}
    emoji_re = re.compile(r"[\U0001F300-\U0001F9FF\U00002600-\U000027BF]+")
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or "=" not in line:
                    continue
                label, _, rest = line.partition("=")
                label = label.strip()
                emojis = emoji_re.findall(rest.strip())
                if not emojis:
                    continue
                canonical = emojis[0]
                for e in emojis:
                    if e not in m:
                        m[e] = (label, canonical)
    except Exception:
        pass
    return m if m else {"😒": ("smirk", "😒"), "🙂": ("smirk", "😒")}

EMOJI_MAP = _load_emoji_mapping()


def get_emoji_list_for_prompt():
    """All emojis from right side of emoji_mapping_raw.txt (LLM allowed set). Maps to 9 target emotions."""
    return " ".join(EMOJI_MAP.keys())


EMOJI_RANGES = [
    (0x1F600, 0x1F64F),
    (0x1F300, 0x1F5FF),
    (0x1F680, 0x1F6FF),
    (0x1F900, 0x1F9FF),
    (0x1FA70, 0x1FAFF),
    (0x2600, 0x26FF),
    (0x2700, 0x27BF),
]


def get_string_no_punctuation_or_emoji(s):
    """去除字符串首尾的空格、标点符号和表情符号"""
    chars = list(s)
    # 处理开头的字符
    start = 0
    while start < len(chars) and is_punctuation_or_emoji(chars[start]):
        start += 1
    # 处理结尾的字符
    end = len(chars) - 1
    while end >= start and is_punctuation_or_emoji(chars[end]):
        end -= 1
    return "".join(chars[start : end + 1])


def is_punctuation_or_emoji(char):
    """检查字符是否为空格、指定标点或表情符号"""
    # 定义需要去除的中英文标点（包括全角/半角）
    punctuation_set = {
        "，",
        ",",  # 中文逗号 + 英文逗号
        "。",
        ".",  # 中文句号 + 英文句号
        "！",
        "!",  # 中文感叹号 + 英文感叹号
        "“",
        "”",
        '"',  # 中文双引号 + 英文引号
        "：",
        ":",  # 中文冒号 + 英文冒号
        "-",
        "－",  # 英文连字符 + 中文全角横线
        "、",  # 中文顿号
        "[",
        "]",  # 方括号
        "【",
        "】",  # 中文方括号
    }
    if char.isspace() or char in punctuation_set:
        return True
    return is_emoji(char)


async def get_emotion(conn, text, send_default=False):
    """获取文本内的情绪消息。LLM emoji -> 映射到 emoji_mapping_raw.txt。若未找到且 send_default，则发送默认。返回 True 若已发送。"""
    emotion = "smirk"
    canonical_emoji = "😒"
    llm_emoji = None
    for char in text:
        if char in EMOJI_MAP:
            llm_emoji = char
            emotion, canonical_emoji = EMOJI_MAP[char]
            break
    if llm_emoji is None and not send_default:
        return False
    try:
        conn.logger.bind(tag=TAG).info(
            f"Emoji mapped: llm={llm_emoji!r} -> emotion={emotion} text={canonical_emoji!r}"
        )
        await conn.websocket.send(
            json.dumps(
                {
                    "type": "llm",
                    "text": canonical_emoji,
                    "emotion": emotion,
                    "session_id": conn.session_id,
                }
            )
        )
        return True
    except Exception as e:
        conn.logger.bind(tag=TAG).warning(f"发送情绪表情失败，错误:{e}")
        return False


def is_emoji(char):
    """检查字符是否为emoji表情"""
    code_point = ord(char)
    return any(start <= code_point <= end for start, end in EMOJI_RANGES)


def check_emoji(text):
    """去除文本中的所有emoji表情"""
    return ''.join(char for char in text if not is_emoji(char) and char != "\n")
