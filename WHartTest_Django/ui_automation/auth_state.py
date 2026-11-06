"""Validate the shape of a Playwright storage snapshot without exposing its credentials."""
from rest_framework.exceptions import ValidationError


def credential_summary(state):
    if state is None:
        state = {}
    if not isinstance(state, dict):
        raise ValidationError('登录态必须是 JSON 对象')

    def entries(obj, key):
        items = obj.get(key, [])
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            raise ValidationError(f'{key} 必须是对象数组')
        return items

    def check_pairs(items, key):
        if any(not isinstance(item.get(field), str) for item in items for field in ('name', 'value')):
            raise ValidationError(f'{key} 每项都需要字符串类型的 name 和 value')

    cookies = entries(state, 'cookies')
    check_pairs(cookies, 'cookies')
    expiry = None
    for cookie in cookies:
        value = cookie.get('expires', -1)
        if type(value) not in (float, int) or not -1 <= value <= 8640000000000:
            raise ValidationError('Cookie expires 必须是有效的时间戳')
        if value > 0 and (expiry is None or value < expiry):
            expiry = value

    summary = {'cookies': len(cookies), 'expires': expiry}
    for field, storage, total in (('origins', 'localStorage', 'local_storage'),
                                  ('session_storage', 'sessionStorage', 'session_storage')):
        count = 0
        for origin in entries(state, field):
            if not isinstance(origin.get('origin'), str):
                raise ValidationError(f'{field} 每项都需要字符串类型的 origin')
            pairs = entries(origin, storage)
            check_pairs(pairs, storage)
            count += len(pairs)
        summary[total] = count
    return summary
