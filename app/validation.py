"""Validation for text, number and date values before SQLite bindings."""
import json
from datetime import date, datetime, time, timezone

from .errors import bad_request

# 便签坐标允许的范围（画布为固定坐标系，超出即为异常输入）
COORDINATE_LIMIT = 20000


def text_value(value, label, maximum, minimum=0, strip=True):
    if not isinstance(value, str):
        raise bad_request(f'{label}必须为文本')
    result = value.strip() if strip else value
    if not minimum <= len(result) <= maximum:
        raise bad_request(f'{label}长度必须在 {minimum} 到 {maximum} 字符之间')
    return result


def optional_text(value, label, maximum, strip=False):
    """可缺失的文本字段：None / 未提供视为空串，其余按 text_value 校验类型与长度。"""
    if value is None:
        return ''
    return text_value(value, label, maximum, 0, strip)


def short_text(value, label, maximum, default=''):
    """短文本字段：空值回落到默认值，非空则校验类型与长度。"""
    if value is None or value == '':
        return default
    return text_value(value, label, maximum, 1)


def number_in_range(value, label, minimum, maximum):
    """数值范围校验；拒绝布尔值和非有限值（NaN 的比较恒为假，会在此被拦下）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise bad_request(f'{label}必须为数字')
    if not minimum <= value <= maximum:
        raise bad_request(f'{label}必须在 {minimum} 到 {maximum} 之间')
    return value


def coordinate(value, label, default=20):
    """便签坐标：不可解析时用默认落点，可解析则限制在画布范围内。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return number_in_range(value, label, -COORDINATE_LIMIT, COORDINATE_LIMIT)


# 任务验收标准：条数与单条长度上限（清单是用来判定的，不是第二个描述字段）
MAX_CRITERIA = 10
CRITERION_MAX = 200


def criteria_value(value) -> str | None:
    """校验并归一化任务的验收标准清单，返回 JSON 字符串。

    接受字符串数组，或带 `done` 标记的对象数组（教师逐条核对时提交的形态）。
    `done` 缺失一律视为未达标：只有显式确认为真才算达标。
    """
    if value is None:
        return None
    if not isinstance(value, list):
        raise bad_request('验收标准需为数组')
    if len(value) > MAX_CRITERIA:
        raise bad_request(f'验收标准最多 {MAX_CRITERIA} 条')
    items = []
    for entry in value:
        if isinstance(entry, str):
            text, done = entry, False
        elif isinstance(entry, dict):
            text, done = entry.get('text'), entry.get('done') is True
        else:
            raise bad_request('验收标准需为文本或 { text, done } 对象')
        text = (text or '').strip()
        if not text:
            raise bad_request('验收标准内容不能为空')
        if len(text) > CRITERION_MAX:
            raise bad_request(f'单条验收标准不超过 {CRITERION_MAX} 字')
        items.append({'text': text, 'done': done})
    return json.dumps(items, ensure_ascii=False)


def due_date(value):
    if value is None or value == '':
        return None
    if not isinstance(value, str) or len(value) > 40:
        raise bad_request('截止日期格式不正确')
    try:
        if len(value) == 10:
            parsed = datetime.combine(date.fromisoformat(value), time.max, timezone.utc)
        else:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if parsed.tzinfo is None:
                raise ValueError('timezone required')
        return parsed.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')
    except (ValueError, OverflowError):
        raise bad_request('截止日期必须为有效日期或带时区的 ISO 时间') from None
