"""Custom template filters."""
from django import template

register = template.Library()


@register.filter
def dictget(d, key):
    """{{ mydict|dictget:somekey }} — safe dict lookup by dynamic key."""
    if d is None:
        return ''
    try:
        return d.get(key, '')
    except AttributeError:
        return ''


@register.filter
def get_item(d, key):
    """Alias for dictget."""
    return dictget(d, key)


@register.filter
def percentage(value, total):
    """Compute percentage safely."""
    try:
        v = float(value or 0)
        t = float(total or 0)
        if t == 0:
            return 0
        return round((v / t) * 100, 1)
    except (TypeError, ValueError):
        return 0


@register.filter
def mul(value, arg):
    try:
        return float(value) * float(arg)
    except (TypeError, ValueError):
        return ''


@register.filter
def div(value, arg):
    try:
        v, a = float(value), float(arg)
        return v / a if a else 0
    except (TypeError, ValueError):
        return ''


@register.filter
def sub(value, arg):
    try:
        return float(value) - float(arg)
    except (TypeError, ValueError):
        return ''


@register.filter
def add_int(value, arg):
    try:
        return int(value) + int(arg)
    except (TypeError, ValueError):
        return value


@register.filter
def split(value, sep=','):
    """Split a string by separator. {{ "a,b,c"|split:"," }}"""
    if value is None:
        return []
    return str(value).split(sep)


@register.filter
def inr(value):
    """Format as Indian Rupees with commas. 123456 -> ₹1,23,456"""
    try:
        v = float(value or 0)
        # Indian comma format
        s = f'{v:,.2f}'
        # Convert western to Indian (groups of 2 after first 3)
        if '.' in s:
            whole, dec = s.split('.')
        else:
            whole, dec = s, '00'
        whole = whole.replace(',', '')
        neg = whole.startswith('-')
        if neg:
            whole = whole[1:]
        if len(whole) > 3:
            last3 = whole[-3:]
            rest = whole[:-3]
            # group rest by 2 from right
            grouped = ''
            while len(rest) > 2:
                grouped = ',' + rest[-2:] + grouped
                rest = rest[:-2]
            grouped = rest + grouped
            formatted = grouped + ',' + last3
        else:
            formatted = whole
        if neg:
            formatted = '-' + formatted
        return f'₹{formatted}'
    except (TypeError, ValueError):
        return f'₹{value}'
