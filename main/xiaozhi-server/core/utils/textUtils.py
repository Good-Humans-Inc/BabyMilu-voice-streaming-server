import json

TAG = __name__
EMOJI_RANGES = [
    (0x1F600, 0x1F64F),
    (0x1F300, 0x1F5FF),
    (0x1F680, 0x1F6FF),
    (0x1F900, 0x1F9FF),
    (0x1FA70, 0x1FAFF),
    (0x2600, 0x26FF),
    (0x2700, 0x27BF),
]
EMOJI_JOINERS = {"\u200d", "\ufe0f", "\ufe0e"}

# Canonical mapping source (in-code, no file read).
CANONICAL_EMOTION_MAP = {
    "smirk": ["😏", "🤨", "😐", "😑", "😶", "😬", "😒", "🙄", "🤔"],
    "heart": ["🥰", "😍", "😘", "🤗", "😍", "😘", "😚", "😙", "🥰", "❤️"],
    "blush": ["😳", "😳", "😊"],
    "sad": ["🙁", "☹️", "😕", "☹️", "😕", "😟", "😨", "😦", "😞", "😧", "🤕", "😮‍💨", "❤️‍🩹"],
    "laugh": ["😄", "😁", "😄", "😁", "😆", "🤣", "😂", "😋", "😛"],
    "sleep": ["😴", "😪", "😪", "😴", "🥱"],
    "starry": ["🤩", "🤩", "🤠", "🥳", "🤯", "😮", "😯", "😲", "❤️‍🔥"],
    "cry": ["😭", "😥", "😢", "😭", "😫", "😩", "😖", "😖", "😣", "💔"],
    "angry": ["😡", "😤", "😠", "😤", "😡", "😠", "🤬", "😒"],
}

EMOJI_MAP = {
    emoji: emotion
    for emotion, emojis in CANONICAL_EMOTION_MAP.items()
    for emoji in emojis
}
_DEFAULT_EMOTION = "smirk"
_DEFAULT_EMOJI = CANONICAL_EMOTION_MAP[_DEFAULT_EMOTION][0]


def _is_skin_tone_modifier(char: str) -> bool:
    cp = ord(char)
    return 0x1F3FB <= cp <= 0x1F3FF


def _extract_emoji_tokens(text: str):
    """Extract emoji tokens, including ZWJ/variation-selector sequences."""
    tokens = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if not is_emoji(ch):
            i += 1
            continue

        token = ch
        i += 1
        while i < n:
            nxt = text[i]
            if nxt == "\u200d" and i + 1 < n:
                token += nxt + text[i + 1]
                i += 2
                continue
            if nxt in EMOJI_JOINERS or _is_skin_tone_modifier(nxt):
                token += nxt
                i += 1
                continue
            break
        tokens.append(token)
    return tokens


def get_allowed_emoji_list_string() -> str:
    """Return space-separated allowed emoji list from canonical mapping."""
    ordered = []
    seen = set()
    for emojis in CANONICAL_EMOTION_MAP.values():
        for emoji in emojis:
            if emoji not in seen:
                ordered.append(emoji)
                seen.add(emoji)
    return " ".join(ordered)


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
    return is_emoji(char) or char in EMOJI_JOINERS or _is_skin_tone_modifier(char)


async def get_emotion(conn, text):
    """获取文本内的情绪消息"""
    emoji = _DEFAULT_EMOJI
    emotion = _DEFAULT_EMOTION
    for token in _extract_emoji_tokens(text):
        if token in EMOJI_MAP:
            emoji = token
            emotion = EMOJI_MAP[token]
            break
    try:
        await conn.websocket.send(
            json.dumps(
                {
                    "type": "llm",
                    "text": emoji,
                    "emotion": emotion,
                    "session_id": conn.session_id,
                }
            )
        )
    except Exception as e:
        conn.logger.bind(tag=TAG).warning(f"发送情绪表情失败，错误:{e}")
    return


def is_emoji(char):
    """检查字符是否为emoji表情"""
    code_point = ord(char)
    return any(start <= code_point <= end for start, end in EMOJI_RANGES)


def check_emoji(text):
    """去除文本中的所有emoji表情"""
    cleaned = text
    for token in _extract_emoji_tokens(text):
        cleaned = cleaned.replace(token, "")
    cleaned = "".join(
        ch
        for ch in cleaned
        if ch not in EMOJI_JOINERS and not _is_skin_tone_modifier(ch) and ch != "\n"
    )
    return cleaned
