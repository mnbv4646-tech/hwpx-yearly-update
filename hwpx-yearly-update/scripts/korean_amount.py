#!/usr/bin/env python3
"""한국어 금액 표기 읽기, 쓰기, 금액 문장 다시 쓰기.

  python3 korean_amount.py parse "6억 5천4백만원"            # -> 654000000 (원)
  python3 korean_amount.py fmt 1860185 --unit 천원            # -> 18억 6천만원
  python3 korean_amount.py fmt 1860185 --unit 천원 --like "2억 3천 5백만원"   # 원래 표기 방식 따르기
  python3 korean_amount.py rate 653665 6577257                # -> 9.94  (증감액, 기준액)
  python3 korean_amount.py sentence "▸ 세입예산액은 20억 9천6백만원으로 ... 증액되었음." \
          --cur 1860185 --prev 2095627 --unit 천원            # 금액, 비율, 증감 방향을 바꾼 문장

규칙
- 금액은 기본적으로 백만원 단위 반올림 (예: 993,160천원 -> 9억 9천3백만원)
- 백만원 미만은 만원 단위 (예: 480천원 -> 48만원)
- 증감률 기본은 소수 둘째 자리 반올림 (--rate-mode trunc 로 버림)
- --like 로 준 원래 표기의 띄어쓰기, 단위 방식(62백만원, 999천원)을 따름
"""
import argparse
import signal
import json
import re
import sys

sys.dont_write_bytecode = True     # skill 폴더에 __pycache__ 를 만들지 않음
from decimal import Decimal, ROUND_HALF_UP, ROUND_DOWN

UNIT_WON = {'원': 1, '천원': 1000, '백만원': 1_000_000, '만원': 10_000}

AMOUNT_RE = re.compile(
    r'(?:\d[\d,]*\s*조\s*)?(?:\d[\d,]*\s*억\s*)?(?:\d+\s*천\s*)?(?:\d+\s*백\s*)?(?:\d+\s*십\s*)?(?:\d[\d,]*\s*)?만?\s*원'
    r'|\d[\d,]*\s*백만원|\d[\d,]*\s*천원')


def find_amounts(s):
    """문장에서 금액 표현의 (시작, 끝, 원 단위 값) 목록."""
    out = []
    for m in AMOUNT_RE.finditer(s):
        txt = m.group(0)
        if not re.search(r'\d', txt):
            continue
        try:
            v = parse(txt)
        except ValueError:
            continue
        # 앞의 공백은 금액에 넣지 않음
        st = m.start() + (len(txt) - len(txt.lstrip()))
        out.append((st, m.end(), v))
    return out


def parse(text):
    """'1조 9,443억 9천5백만원' -> 원 단위 정수"""
    s = re.sub(r'\s+', '', text)
    m = re.fullmatch(r'([\d,]+)백만원', s)
    if m:
        return int(m.group(1).replace(',', '')) * 1_000_000
    m = re.fullmatch(r'([\d,]+)천원', s)
    if m:
        return int(m.group(1).replace(',', '')) * 1000
    m = re.fullmatch(r'(?:([\d,]+)조)?(?:([\d,]+)억)?(?:(\d+)천)?(?:(\d+)백)?(?:(\d+)십)?(?:([\d,]+))?(만)?원', s)
    if not m or not any(m.groups()[:6]):
        raise ValueError(f'금액으로 읽을 수 없음: {text}')
    jo, eok, cheon, baek, sip, ones, man = m.groups()
    n = lambda x: int(x.replace(',', '')) if x else 0
    if man:
        small = n(cheon) * 1000 + n(baek) * 100 + n(sip) * 10 + n(ones)
        return n(jo) * 10 ** 12 + n(eok) * 10 ** 8 + small * 10 ** 4
    if cheon or baek or sip:
        raise ValueError(f'금액으로 읽을 수 없음: {text}')
    return n(jo) * 10 ** 12 + n(eok) * 10 ** 8 + n(ones)


def infer_style(sample):
    st = {'jo_space': True, 'eok_space': True, 'cheon_baek_space': False, 'mode': 'korean', 'won_space': False}
    if not sample:
        return st
    s = sample.strip()
    if re.fullmatch(r'(?:\d{2,}|\d{1,3}(?:,\d{3})+)\s*백만원', s):   # '62백만원' (한 자리 '2백만원'은 일반 표기)
        st['mode'] = 'baekman'
    elif re.fullmatch(r'[\d,]+\s*천원', s):
        st['mode'] = 'cheon'
    if re.search(r'조\d', s):
        st['jo_space'] = False
    if re.search(r'억\d', s):
        st['eok_space'] = False
    if re.search(r'천\s+\d+백', s):
        st['cheon_baek_space'] = True
    if re.search(r'\S\s+원$', s):
        st['won_space'] = True
    mm = re.fullmatch(r'([\d,]+)\s*만\s*원', s)
    if mm and int(mm.group(1).replace(',', '')) >= 100:
        st['mode'] = 'man'      # '500만 원'처럼 백만원이 넘는데도 만원으로 쓴 문서. '48만원'은 금액이 작아서 쓴 것이라 문체로 보지 않음
    elif re.fullmatch(r'[\d,]+\s*원', s):
        st['mode'] = 'won'
    return st


def round_div(v, unit, mode='round'):
    q = Decimal(v) / Decimal(unit)
    return int(q.quantize(Decimal(1), rounding=ROUND_HALF_UP if mode == 'round' else ROUND_DOWN))


def fmt_won(won, like=None, mode='round'):
    """원 단위 정수 -> 한국어 금액 (부호 없이)"""
    won = abs(int(won))
    st = infer_style(like)
    if st['mode'] == 'baekman':
        return f'{round_div(won, 1_000_000, mode):,}백만원'
    if st['mode'] == 'cheon':
        return f'{round_div(won, 1000, mode):,}천원'
    W = ' 원' if st['won_space'] else '원'
    if st['mode'] == 'man':
        return f'{round_div(won, 10_000, mode):,}만' + W
    if st['mode'] == 'won':
        return f'{won:,}' + W
    if won < 1_000_000:                          # 백만원 미만은 만원 단위 (500천원 → 50만원)
        man = round_div(won, 10_000, mode)
        return f'{man:,}만' + W if man else f'{won:,}' + W
    m = round_div(won, 1_000_000, mode)          # 백만원 단위
    jo, rest = divmod(m, 1_000_000)
    eok, low = divmod(rest, 100)
    cheon, baek = divmod(low, 10)
    parts = []
    if jo:
        parts.append(f'{jo:,}조')
    if eok:
        parts.append(f'{eok:,}억')
    small = ''
    if cheon:
        small += f'{cheon}천'
    if baek:
        small += (' ' if (cheon and st['cheon_baek_space']) else '') + f'{baek}백'
    if small:
        small += '만'
    out = ''
    for i, ptxt in enumerate(parts):
        if i:
            out += ' ' if (parts[i - 1].endswith('조') and st['jo_space']) or (parts[i - 1].endswith('억') and st['eok_space']) else ''
        out += ptxt
    if small:
        if out:
            out += ' ' if st['eok_space'] else ''
        out += small + W
    else:
        out += W
    return out


def fmt(value, unit='천원', like=None, mode='round'):
    return fmt_won(int(Decimal(str(value).replace(',', '').replace('△', '-')) * UNIT_WON[unit]), like, mode)


def to_num(x):
    if isinstance(x, (int, float)):
        return x
    s = str(x).strip().replace(',', '')
    neg = s.startswith('△') or s.startswith('-')
    s = s.lstrip('△-')
    v = Decimal(s) if s else Decimal(0)
    return -v if neg else v


def rate(diff, base, digits=2, mode='round', sign='△'):
    """증감률 문자열. 기준이 0이면 '-'"""
    diff, base = Decimal(str(to_num(diff))), Decimal(str(to_num(base)))
    if base == 0:
        return '-'
    r = diff / base * 100
    q = Decimal(1).scaleb(-digits)
    r = r.quantize(q, rounding=ROUND_HALF_UP if mode == 'round' else ROUND_DOWN)
    s = f'{abs(r):,.{digits}f}'
    return (sign if r < 0 else '') + s


def fmt_num(v, sign='△'):
    v = int(to_num(v))
    return (sign if v < 0 else '') + f'{abs(v):,}'


RATE_RE = re.compile(r'(\d[\d,]*(?:\.\d+)?)\s*%')


def rewrite_sentence(sent, cur, prev, unit='천원', digits=None, rate_mode='round', amount_mode='round'):
    """금액 문장의 금액, 증감률, 증가/감소, 증액/감액을 새 값으로 바꾼다.

    문장 안 첫 금액 = 올해 금액, 마지막 금액 = 증감액, 첫 % = 증감률 로 본다.
    읽을 수 없거나 증감이 0인 경우 None과 사유를 돌려준다.
    """
    cur, prev = to_num(cur), to_num(prev)
    diff = cur - prev
    amts = find_amounts(sent)
    if not amts:
        return None, '금액 표현을 찾지 못함'
    if diff == 0:
        return None, '증감이 0 (문장 형식을 사람이 정해야 함, 예: "전년도와 같음")'
    up = diff > 0
    pieces = []
    last = 0
    targets = [(amts[0][0], amts[0][1], fmt(cur, unit, sent[amts[0][0]:amts[0][1]], amount_mode))]
    if len(amts) >= 2:
        a = amts[-1]
        targets.append((a[0], a[1], fmt(abs(diff), unit, sent[a[0]:a[1]], amount_mode)))
    m = RATE_RE.search(sent)
    if m:
        d = digits if digits is not None else (len(m.group(1).split('.')[1]) if '.' in m.group(1) else 0)
        targets.append((m.start(1), m.end(1), rate(diff, prev, d, rate_mode).lstrip('△')))
    targets.sort()
    out = ''
    for st, en, rep in targets:
        out += sent[last:st] + rep
        last = en
    out += sent[last:]
    swap = [('증가', '감소'), ('증액', '감액'), ('늘어난', '줄어든'), ('상승', '하락')]
    for a, b in swap:
        want, other = (a, b) if up else (b, a)
        out = out.replace(other, '\x00').replace('\x00', want)
    return out, None


def main():
    try:
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('parse')
    s.add_argument('text')
    s = sub.add_parser('fmt')
    s.add_argument('value')
    s.add_argument('--unit', default='천원', choices=list(UNIT_WON))
    s.add_argument('--like')
    s.add_argument('--mode', default='round', choices=['round', 'trunc'])
    s = sub.add_parser('rate')
    s.add_argument('diff')
    s.add_argument('base')
    s.add_argument('--digits', type=int, default=2)
    s.add_argument('--mode', default='round', choices=['round', 'trunc'])
    s = sub.add_parser('sentence')
    s.add_argument('text')
    s.add_argument('--cur', required=True)
    s.add_argument('--prev', required=True)
    s.add_argument('--unit', default='천원', choices=list(UNIT_WON))
    s.add_argument('--digits', type=int)
    s.add_argument('--rate-mode', default='round', choices=['round', 'trunc'])
    s = sub.add_parser('batch', help='JSON 목록 [{"text","cur","prev","unit"}] 을 표준입력으로 받아 한꺼번에 처리')
    a = ap.parse_args()
    if a.cmd == 'parse':
        print(parse(a.text))
    elif a.cmd == 'fmt':
        print(fmt(a.value, a.unit, a.like, a.mode))
    elif a.cmd == 'rate':
        print(rate(a.diff, a.base, a.digits, a.mode))
    elif a.cmd == 'sentence':
        out, why = rewrite_sentence(a.text, a.cur, a.prev, a.unit, a.digits, a.rate_mode)
        print(out if out else f'[다시 쓰지 못함] {why}')
    elif a.cmd == 'batch':
        items = json.load(sys.stdin)
        res = []
        for it in items:
            out, why = rewrite_sentence(it['text'], it['cur'], it['prev'], it.get('unit', '천원'), it.get('digits'), it.get('rate_mode', 'round'))
            res.append({'text': it['text'], 'new': out, 'problem': why})
        print(json.dumps(res, ensure_ascii=False, indent=1))


if __name__ == '__main__':
    main()
