import re
from typing import Optional


def parse_chinese_time(text: str) -> Optional[int]:
    """
    从中文文本中解析时间，返回总秒数。

    支持格式：
    - "1小时48秒"       -> 3648
    - "2小时30分钟"     -> 9000
    - "15分30秒"        -> 930
    - "还需时间1小时48秒" -> 3648
    - "3天2小时"        -> 262800
    """
    total = 0
    found = False

    day_match = re.search(r'(\d+)\s*(?:天|д(?:ень|ня|ней)?\b|сут(?:ки|ок)?\b)', text, re.I)
    hour_match = re.search(r'(\d+)\s*(?:小时|ч(?:ас(?:а|ов)?)?\b)', text, re.I)
    min_match = re.search(r'(\d+)\s*(?:分(?:钟)?|мин(?:ут(?:а|ы|у)?)?\b|м\b)', text, re.I)
    sec_match = re.search(r'(\d+)\s*(?:秒|сек(?:унд(?:а|ы|у)?)?\b|с\b)', text, re.I)

    if day_match:
        total += int(day_match.group(1)) * 86400
        found = True
    if hour_match:
        total += int(hour_match.group(1)) * 3600
        found = True
    if min_match:
        total += int(min_match.group(1)) * 60
        found = True
    if sec_match:
        total += int(sec_match.group(1))
        found = True

    return total if found else None


def format_seconds(seconds: int) -> str:
    """将秒数格式化为人类可读字符串"""
    if seconds <= 0:
        return '0 с'
    parts = []
    if seconds >= 3600:
        h = seconds // 3600
        parts.append(f'{h} ч')
        seconds %= 3600
    if seconds >= 60:
        m = seconds // 60
        parts.append(f'{m} мин')
        seconds %= 60
    if seconds > 0:
        parts.append(f'{seconds} с')
    return ' '.join(parts)
