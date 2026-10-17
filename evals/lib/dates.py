# -*- coding: utf-8 -*-
"""
Date template resolver for Sewa Setu evaluation personas.
All dates are resolved in Indian Standard Time (IST, Asia/Kolkata).
"""

import re
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def get_today_ist() -> date:
    return datetime.now(IST).date()


def resolve_date_expr(expr: str, ref_date: date | None = None) -> date:
    """
    Resolve expressions like:
      - 'today'
      - 'today-70y'
      - 'today-60y+7d'
      - 'today-14d'
      - 'today-90d'
    """
    if ref_date is None:
        ref_date = get_today_ist()

    s = expr.strip().lower()
    if s == "today":
        return ref_date

    m = re.match(r"^today(?:([+-])(\d+)y)?(?:([+-])(\d+)d)?$", s)
    if not m:
        # If already YYYY-MM-DD or DD/MM/YYYY
        if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
            return datetime.strptime(s, "%Y-%m-%d").date()
        if re.match(r"^\d{2}/\d{2}/\d{4}$", s):
            return datetime.strptime(s, "%d/%m/%Y").date()
        raise ValueError(f"Unrecognized date template expression: {expr}")

    y_sign, y_val, d_sign, d_val = m.groups()
    res = ref_date

    if y_val:
        years = int(y_val)
        try:
            if y_sign == "-":
                res = res.replace(year=res.year - years)
            else:
                res = res.replace(year=res.year + years)
        except ValueError:
            # Handle Feb 29 leap year
            if y_sign == "-":
                res = date(res.year - years, res.month, 28)
            else:
                res = date(res.year + years, res.month, 28)

    if d_val:
        days = int(d_val)
        delta = timedelta(days=days)
        if d_sign == "-":
            res = res - delta
        else:
            res = res + delta

    return res


def resolve_template_sql(sql_str: str, ref_date: date | None = None) -> str:
    """
    Find and replace all 'today[+-...]' patterns in a SQL string.
    """
    if ref_date is None:
        ref_date = get_today_ist()

    def _repl(match):
        expr = match.group(1)
        resolved = resolve_date_expr(expr, ref_date)
        return f"'{resolved.strftime('%Y-%m-%d')}'"

    return re.sub(r"'(today(?:[+-]\d+[yd])*)'", _repl, sql_str)
