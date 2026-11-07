#!/usr/bin/env python3
"""PDF의 표 같은 쪽을 '들여쓰기 단계 + 숫자 열' 행으로 읽기.

예산서 사업명세서처럼 들여쓰기로 단계를 나타내고 오른쪽에 숫자 열이 있는 쪽을
글자 위치(x 좌표)로 읽어, 행마다 단계(L0, L1 ...), 이름, 열 값, 메모를 돌려준다.
쪽 단위 글(source_index.py pages)보다 행과 열이 정확하다.

  python3 pdf_rows.py IDX 834-860                    # 인쇄 쪽번호 범위
  python3 pdf_rows.py IDX 834-860 --max-level 3      # L3 까지만 (예: 세부사업까지)
  python3 pdf_rows.py IDX 834-960 --find 아트 클래스  # 찾은 행과 그 위 단계 경로, 아래 행
  python3 pdf_rows.py IDX 834-860 --json rows.json   # 행 목록을 JSON 으로 (id, parent, path 포함)
  python3 pdf_rows.py IDX 834-860 --gone             # 올해 없어진 하위 항목이 있는 행 (전년 금액 차이)
  python3 pdf_rows.py IDX 833-857 --changes --label '^[0-9]{3}' --min 10000 --top 5   # 편성목 증감 큰 순
  python3 pdf_rows.py IDX 833-857 --new              # 전년 0 인 새 항목 (가장 위 단계만, 산출근거 요약)
  python3 pdf_rows.py IDX 1552-1575 --spread         # 두 쪽에 걸친 표 (계속비 사업조서: 왼쪽 쪽 이름, 오른쪽 쪽 숫자)
  python3 pdf_rows.py IDX 1-20 --pdf                 # PDF 쪽번호(전체 기준)
  python3 pdf_rows.py --file 설명자료.pdf 3-9        # 색인 없이 PDF 하나를 PDF 쪽번호로

출력 예:
  # p834 열: 예산액 | 전년도 예산액 | 비교증감
  p834 L3       공연·전시 지원 | 46,300 | 47,500 | △1,200
  p834 L6             ο중형승합 |  2,000 |  |  ‖ 2,000,000원*1대

- 단계 번호는 그 범위에서 숫자가 있는 행들의 왼쪽 시작 위치를 모아 매긴다.
  같은 문서라도 범위를 바꾸면 번호가 달라질 수 있으니 이름 앞 기호와 함께 본다.
- '‖' 뒤는 숫자 열이 아닌 오른쪽 글(산출 근거 등)이다.
- 쪽 위쪽 머리글(부서:, 정책: 등)은 '#' 줄로, 바뀔 때만 보여 준다.
- 이 도구가 읽은 값도 원자료 대조(verify.py)를 거친다.
"""
import argparse
import gc
import json
import os
import re
import signal
import sys

sys.dont_write_bytecode = True     # skill 폴더에 __pycache__ 를 만들지 않음
from statistics import median

NUM_WORD = re.compile(r'^\(?[△▲\-]?\d[\d,]*(?:\.\d+)?%?\)?$')
PRINTED_LINE = re.compile(r'^\s*[-–]\s*\d{1,4}\s*[-–]\s*$|^\s*\d{1,4}\s*(/\s*\d{1,4})?\s*$')
SEG_GAP = 9.0       # 이보다 넓은 틈이면 다른 칸
LINE_TOL = 2.6      # 같은 줄로 보는 위아래 차이
LEVEL_TOL = 1.2       # 같은 단계로 보는 왼쪽 위치 차이 (글자 시작 위치는 보통 소수점까지 같음)
COL_TOL = 9.0       # 같은 열로 보는 오른쪽 끝 위치 차이
BULLETS = '○ο∘•·ㆍ▪▫■□◦-‐–※▶▷►◆◇★☆*'


def lines_of(page):
    words = page.extract_words(keep_blank_chars=False, use_text_flow=False)
    words.sort(key=lambda w: (w['top'], w['x0']))
    lines = []
    for w in words:
        if lines and abs(lines[-1]['top'] - w['top']) <= LINE_TOL:
            lines[-1]['words'].append(w)
        else:
            lines.append({'top': w['top'], 'words': [w]})
    for ln in lines:
        ln['words'].sort(key=lambda w: w['x0'])
        segs = []
        for w in ln['words']:
            if segs and w['x0'] - segs[-1][-1]['x1'] <= SEG_GAP:
                segs[-1].append(w)
            else:
                segs.append([w])
        # 숫자끼리 붙어 있는 조각('△249,973,000 △92,946,000')은 숫자 하나씩 나눔
        split = []
        for sg in segs:
            if len(sg) > 1 and all(NUM_WORD.match(w['text']) for w in sg):
                split.extend([w] for w in sg)
            else:
                split.append(sg)
        segs = split
        ln['segs'] = [{'text': ' '.join(w['text'] for w in s), 'x0': s[0]['x0'], 'x1': s[-1]['x1'],
                       'num': all(NUM_WORD.match(w['text']) for w in s) and len(s) == 1} for s in segs]
        ln['text'] = ' '.join(s['text'] for s in ln['segs'])
    return lines


def cluster(values, tol):
    """1차원 값 묶기. [(대표값, 개수)] 를 위치 순으로."""
    out = []
    for v in sorted(values):
        if out and v - out[-1][-1] <= tol:
            out[-1].append(v)
        else:
            out.append([v])
    return [(median(c), len(c)) for c in out]


def cluster_peaks(values, tol):
    """자주 나오는 값부터 중심으로 삼아 묶기 (값이 촘촘히 이어져도 한 덩어리로 번지지 않게)."""
    from collections import Counter
    cnt = Counter(round(v, 1) for v in values)
    centers = []
    for v, n in sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0])):
        for c in centers:
            if abs(c[0] - v) <= tol:
                c[1] += n
                break
        else:
            centers.append([v, n])
    return sorted((c, n) for c, n in centers)


def nearest(clusters, v, tol):
    best = None
    for i, (c, _) in enumerate(clusters):
        d = abs(c - v)
        if d <= tol and (best is None or d < best[1]):
            best = (i, d)
    return None if best is None else best[0]


def read_pages(path, page_numbers):
    """page_numbers: [(pdf 쪽(1부터), 표시 쪽)]"""
    import pdfplumber
    out = []
    for s in range(0, len(page_numbers), 30):
        chunk = page_numbers[s:s + 30]
        with pdfplumber.open(path, pages=[p for p, _ in chunk]) as pdf:
            for pg, (_, shown) in zip(pdf.pages, chunk):
                out.append((shown, pg.width, lines_of(pg)))
                pg.flush_cache()
        gc.collect()
    return out


def page_columns(lines, page_w):
    """숫자 칸의 오른쪽 끝을 묶어 열을 정하고, 바로 위 머리글 글자로 이름을 붙인다.
    돌려주는 값: (열 위치, 열 이름, 머리글 맨 아래 줄 위치)"""
    xs = [s['x1'] for ln in lines for s in ln['segs'] if s['num'] and s['x0'] > page_w * 0.3]
    cols = [c for c, n in cluster(xs, COL_TOL) if n >= 2]
    if not cols:
        return [], [], None
    first_num_top = min(ln['top'] for ln in lines if any(s['num'] and s['x0'] > page_w * 0.3 for s in ln['segs']))
    names = [[] for _ in cols]
    centers = []
    for c in cols:
        cs = [(s['x0'] + s['x1']) / 2 for ln in lines for s in ln['segs'] if s['num'] and abs(s['x1'] - c) <= COL_TOL]
        centers.append(median(cs))
    header_bottom = None
    for ln in lines:
        if not (first_num_top - 48 <= ln['top'] < first_num_top - 1):
            continue
        for s in ln['segs']:
            if s['num'] or s['text'].replace(' ', '').startswith('(단위'):
                continue
            cx = (s['x0'] + s['x1']) / 2
            i = min(range(len(cols)), key=lambda k: abs(centers[k] - cx))
            if abs(centers[i] - cx) <= 60 and s['x0'] > page_w * 0.3:
                names[i].append(s['text'])
                header_bottom = ln['top'] if header_bottom is None else max(header_bottom, ln['top'])
    return cols, [' '.join(n) or f'열{i + 1}' for i, n in enumerate(names)], header_bottom


TOTAL_LABEL = re.compile(r'^\s*(총\s*계|합\s*계|소\s*계|계|총\s*괄)\s*$|^\s*<.*>\s*$')   # 합계 행, '< 복지정책과 >' 같은 구분 표시
HEAD_KV = re.compile(r'^\S{1,6}\s*:')
FORMULA = re.compile(r'^[(（]?[△\-]?\d[\d,]*(?:\.\d+)?\s*(?:원|천원|백만원|%|명|개|회|월|일|식|건|대|㎡|m)(?=\s*[*×xX+\-=/)]|\s*$)')   # 산출식처럼 숫자+단위로 시작하는 글


def build_rows(pages):
    """pages: [(표시 쪽, 너비, lines)] → 행 목록."""
    raw = []
    last = None
    for shown, w, lines in pages:
        if not lines:
            continue
        cols, names, header_bottom = page_columns(lines, w)
        if last and cols and (header_bottom is None or len(cols) < len(last[0])) and \
                all(nearest([(c, 0) for c in last[0]], x, COL_TOL) is not None for x in cols):
            cols, names = last          # 앞쪽과 같은 양식인데 일부 열만 쓰인 쪽: 앞쪽 열 이름을 이어 씀
        elif cols and header_bottom is not None:
            last = (cols, names)
        nums = [ln['top'] for ln in lines if any(s['num'] and s['x0'] > w * 0.3 for s in ln['segs'])]
        first_num_top = min(nums) if nums else None
        bottom = max(l['top'] for l in lines)
        for ln in lines:
            if PRINTED_LINE.match(ln['text']) and ln['top'] > 0.85 * bottom:
                continue
            if header_bottom is not None:
                head = ln['top'] <= header_bottom + 1
            else:
                head = first_num_top is None or ln['top'] < first_num_top - 1
            vals = [''] * len(cols)
            label, notes = '', []
            segs = ln['segs']
            lead = segs[0]
            start = 0
            if not (lead['num'] and lead['x0'] > w * 0.3) and lead['x0'] < w * 0.3 and not FORMULA.match(lead['text']):
                label = lead['text']
                start = 1
                # 글자 사이를 띄운 이름 ('합   계', '환 경 국'): 짧은 조각이 이어지면 붙임
                while start < len(segs) and len(label.replace(' ', '')) <= 12 and not segs[start]['num'] \
                        and len(segs[start]['text'].replace(' ', '')) <= 2 and len(segs[start - 1]['text'].replace(' ', '')) <= 2 \
                        and segs[start]['x0'] - segs[start - 1]['x1'] < 40 and re.match(r'[가-힣]', segs[start]['text']):
                    label = label.replace(' ', '') + segs[start]['text'].replace(' ', '')
                    start += 1
            for s in segs[start:]:
                ci = nearest([(c, 0) for c in cols], s['x1'], COL_TOL) if s['num'] and s['x0'] > w * 0.3 else None
                if ci is not None and not vals[ci]:
                    vals[ci] = s['text']
                else:
                    notes.append(s['text'])
            raw.append({'page': shown, 'top': round(ln['top'], 1), 'x': round(lead['x0'], 1),
                        'label': label, 'cols': names, 'vals': vals, 'note': ' '.join(notes),
                        'head': head, 'has_num': any(vals), 'text': ln['text']})
    body = [r for r in raw if not r['head'] and r['label']]
    # 숫자가 있는 행(또는 바로 아래 줄에 산출식과 숫자가 이어지는 행)의 시작 위치가 단계
    numbered = []
    for i, r in enumerate(raw):
        if r['head'] or not r['label']:
            continue
        nxt = raw[i + 1] if i + 1 < len(raw) else None
        if r['has_num'] or (nxt and nxt['page'] == r['page'] and not nxt['label'] and nxt['has_num']):
            numbered.append(r['x'])
    peaks = cluster_peaks(numbered, LEVEL_TOL)
    min_n = 2 if len(numbered) >= 50 else 1
    levels = [(c, 0) for c, n in peaks if n >= min_n] or [(c, 0) for c, n in peaks]
    level_of = lambda r: nearest(levels, r['x'], LEVEL_TOL)
    bullet_share = {}
    for r in body:
        lv = level_of(r)
        if lv is not None:
            b = bullet_share.setdefault(lv, [0, 0])
            b[0] += r['label'][:1] in BULLETS
            b[1] += 1
    gaps = [b['top'] - a['top'] for a, b in zip(raw, raw[1:]) if a['page'] == b['page'] and 0 < b['top'] - a['top'] < 40]
    step = median(gaps) if gaps else 12
    rows = []
    for ri, r in enumerate(raw):
        if r['head']:
            r['level'] = None
            rows.append(r)
            continue
        prev = next((q for q in reversed(rows) if not q['head']), None)
        close = prev is not None and prev['page'] == r['page'] and r['top'] - prev['top'] <= step * 1.8
        lv = level_of(r) if r['label'] and not TOTAL_LABEL.match(r['label']) else None   # 가운데 놓인 '총계'는 단계 밖
        cont = False
        only_first = r['has_num'] and r['vals'][0] and not any(r['vals'][1:])
        if prev is not None and not r['label']:
            cont = True             # 이름 없이 오른쪽 글과 숫자만 있는 줄: 앞 행의 이어짐 (쪽이 바뀌어도)
        elif close and prev['has_num'] and only_first and r['note'] and len(r['note']) <= 3 and \
                len(prev['vals']) > 1 and all(prev['vals'][:2]):
            cont = True             # 두 줄 이름의 둘째 줄에 재원 줄(국 2,175,000)이 같이 있는 경우
        elif close and not r['has_num'] and not r['note'] and r['label'] and unfinished(prev['label']):
            cont = True             # 앞 행 이름이 괄호나 '및' 등으로 끝나 다음 줄로 이어짐
        elif close and prev['has_num'] and not r['has_num'] and not r['note'] and r['label'] and \
                abs(r['x'] - prev['x']) <= LEVEL_TOL and len(prev['vals']) > 1 and all(prev['vals'][:2]):
            nxt = raw[ri + 1] if ri + 1 < len(raw) else None
            if nxt and nxt['page'] == r['page'] and not nxt['label'] and nxt['has_num'] and nxt['vals'][0] \
                    and not any(nxt['vals'][1:]):
                cont = True         # 두 줄 이름의 둘째 줄 (아래에 재원 줄이 이어짐)
        elif close and not prev['has_num'] and r['label'] and r['x'] > prev['x'] + LEVEL_TOL \
                and prev['label'][:1] in BULLETS and r['label'][:1] not in BULLETS and not re.match(r'^\d', r['label']):
            cont = True             # 기호로 시작한 항목 이름이 다음 줄로 넘어감
        elif close and not prev['has_num'] and r['label'] and r['x'] > prev['x'] + LEVEL_TOL:
            share = bullet_share.get(lv, [0, 1])
            if lv is None or (r['label'][:1] not in BULLETS and share[0] / share[1] > 0.6):
                cont = True         # 이름이 두 줄로 나뉜 행
        if cont:
            if r['label']:
                prev['label'] = join_label(prev['label'], r['label'])
            if r['has_num'] and prev['has_num'] and len(prev['vals']) == len(r['vals']) and \
                    any(a and b for a, b in zip(prev['vals'], r['vals'])):
                # 이미 값이 있는 행 아래의 재원 줄 (국 31,943 / 시 2,653,454 등)
                prev.setdefault('subs', []).append((r['note'] + ' ' + ' '.join(v for v in r['vals'] if v)).strip())
                continue
            if r['has_num'] and len(prev['vals']) == len(r['vals']):
                prev['vals'] = [a or b for a, b in zip(prev['vals'], r['vals'])]
                prev['has_num'] = True
            elif r['has_num']:
                prev['vals'], prev['cols'], prev['has_num'] = r['vals'], r['cols'], True
            if r['note']:
                prev['note'] = (prev['note'] + ' ' + r['note']).strip()
            continue
        r['level'] = lv
        rows.append(r)
    stack = []
    keys_prev = None
    page_heads = {}
    for r in rows:
        if r['head'] and HEAD_KV.match(r['text']):
            page_heads.setdefault(r['page'], set()).update(re.findall(r'(\S{1,6})\s*:', r['text']))
    cur_page = None
    for i, r in enumerate(rows):
        r['id'] = i
        r['parent'] = None
        if r['page'] != cur_page:
            cur_page = r['page']
            keys = frozenset(page_heads.get(cur_page, ()))
            if keys and keys_prev is not None and keys != keys_prev:
                stack = []          # 쪽 머리글 종류가 바뀌면(세입 → 세출 등) 다른 표로 보고 단계를 새로 시작
            if keys:
                keys_prev = keys
        if r['head'] or r['level'] is None:
            r['path'] = [s['label'] for s in stack]
            continue
        while stack and stack[-1]['level'] >= r['level']:
            stack.pop()
        r['path'] = [s['label'] for s in stack]
        r['parent'] = stack[-1]['id'] if stack else None
        stack.append(r)
    return rows, levels


def unfinished(label):
    return label.count('(') > label.count(')') or bool(re.search(r'(ㆍ|·|,|및|\()$', label.strip()))


def join_label(a, b):
    """두 줄로 나뉜 이름 잇기. 한글은 낱말 중간에서도 줄이 바뀌므로('조' + '성사업') 한글끼리는 붙인다."""
    if not a:
        return b
    if re.search(r'[가-힣ㆍ·(/]$', a) and re.match(r'[가-힣)]', b):
        return a + b
    return a + ' ' + b


def fmt_row(r, widths=None):
    ind = '  ' * (r['level'] or 0)
    lv = f"L{r['level']}" if r['level'] is not None else '  '
    vals = ' | '.join(r['vals'])
    s = f"p{r['page']} {lv} {ind}{r['label']}"
    if r['vals']:
        s += ' | ' + vals
    if r.get('subs'):
        s += ' [' + ' / '.join(r['subs']) + ']'
    if r['note']:
        s += ' ‖ ' + r['note']
    return s


def to_num(t):
    t = t.strip('()%')
    neg = t[:1] in '△▲-'
    try:
        v = float(t.lstrip('△▲-').replace(',', ''))
    except ValueError:
        return None
    return -v if neg else v


def check_identity(rows):
    """세 열이 '올해, 전년, 증감' 꼴이면 올해 - 전년 = 증감 인지 확인해 읽기 오류를 알린다."""
    three = [r for r in rows if not r['head'] and len(r['vals']) == 3 and all(r['vals'])]
    ok, bad = 0, []
    for r in three:
        a, b, c = (to_num(v) for v in r['vals'])
        if None in (a, b, c):
            continue
        if abs(a - b - c) < 0.5:
            ok += 1
        else:
            bad.append(r)
    if three and ok >= 0.8 * len(three) and bad:
        print(f'# 주의: 첫 열 - 둘째 열 = 셋째 열 이 맞지 않는 행 {len(bad)}개 (읽기 오류일 수 있으니 쪽 글로 확인)', file=sys.stderr)
        for r in bad[:10]:
            print('#   ' + fmt_row(r).strip(), file=sys.stderr)
    # 아래 단계 합 = 위 단계 값 (첫 열 기준). 대부분 맞는 문서에서만 어긋난 곳을 알린다.
    kids = {}
    for r in rows:
        if r.get('parent') is not None and r['vals'] and r['vals'][0]:
            kids.setdefault(r['parent'], []).append(r)
    last_desc = {}
    for r in rows:
        q = r.get('parent')
        while q is not None:
            last_desc[q] = r['id']
            q = rows[q].get('parent')
    end_id = max((r['id'] for r in rows), default=-1)
    ok, bad = 0, []
    for pid, ch in kids.items():
        p = rows[pid]
        if not p['vals'] or not p['vals'][0] or last_desc.get(pid) == end_id:
            continue                # 범위 끝까지 이어지는 행은 아래 행이 범위 밖에 더 있을 수 있음
        pv = to_num(p['vals'][0])
        sv = sum(to_num(c['vals'][0]) or 0 for c in ch)
        if pv is None:
            continue
        if abs(pv - sv) < 0.5:
            ok += 1
        else:
            bad.append((p, sv))
    if ok + len(bad) >= 5 and ok >= 0.8 * (ok + len(bad)) and bad:
        print(f'# 주의: 아래 단계 합이 위 단계 값과 다른 행 {len(bad)}개 (줄을 잘못 읽었거나 쪽이 범위 밖에 있음)', file=sys.stderr)
        for p, sv in bad[:10]:
            print(f"#   {fmt_row(p).strip()}  (아래 합 {sv:,.0f})", file=sys.stderr)


def gone_items(rows):
    """전년 금액이 아래 단계 전년 합보다 큰 행: 올해 없어진 하위 항목의 전년 금액으로 보인다."""
    kids = {}
    for r in rows:
        if r.get('parent') is not None:
            kids.setdefault(r['parent'], []).append(r)
    out = []
    for pid, ch in kids.items():
        p = rows[pid]
        if len(p['vals']) < 2 or not p['vals'][1]:
            continue
        pv = to_num(p['vals'][1])
        with_prev = [c for c in ch if len(c['vals']) > 1 and c['vals'][1]]
        if pv is None or not with_prev:
            continue
        sv = sum(to_num(c['vals'][1]) or 0 for c in with_prev)
        if pv - sv >= 0.5:
            out.append((p, pv - sv))
    return out


def three(r):
    """올해, 전년, 증감 세 숫자가 있는 행이면 (올해, 전년, 증감)."""
    if r.get('head') or len(r['vals']) < 3 or not r['vals'][0] or not r['vals'][1] or not r['vals'][2]:
        return None                 # 산출근거 줄처럼 전년, 증감 칸이 빈 행은 제외
    a, b, c = (to_num(v) if v not in ('', '-') else 0 for v in r['vals'][:3])
    if None in (a, b, c):
        return None
    return a, b, c


def print_changes(rows, label_re=None, min_abs=0, top=None):
    """증감액이 큰 행: 증액(전년이 있는 것)과 감액을 따로 큰 순서로."""
    cand = []
    for r in rows:
        t = three(r)
        if not t or (label_re and not re.search(label_re, r['label'])):
            continue
        cur, prev, diff = t
        if abs(diff) >= min_abs and diff != 0:
            cand.append((r, cur, prev, diff))
    ups = sorted([x for x in cand if x[3] > 0 and x[2] > 0], key=lambda x: -x[3])
    downs = sorted([x for x in cand if x[3] < 0], key=lambda x: x[3])
    news = [x for x in cand if x[3] > 0 and x[2] == 0]
    for title, lst in (('증액 (전년 있음)', ups), ('감액', downs), ('전년 0 (신규 후보, --new 로 자세히)', news)):
        print(f'== {title} {len(lst)}건' + (f', 위 {top}건' if top else ''))
        for r, cur, prev, diff in lst[:top]:
            print(f"   {r['vals'][2]:>12} | {' > '.join(r['path'][-2:] + [r['label']])} | 올해 {r['vals'][0]} 전년 {r['vals'][1]} | p{r['page']}")


def print_new(rows, label_re=None):
    """전년이 0인 행 중 가장 위 단계만 (새 세부사업이면 그 한 줄, 아래 편성목은 묶음)."""
    new_ids = set()
    for r in rows:
        t = three(r)
        if t and t[1] == 0 and t[0] > 0:
            new_ids.add(r['id'])
    tops = [r for r in rows if r['id'] in new_ids and r.get('parent') not in new_ids
            and (not label_re or re.search(label_re, r['label']))]
    print(f'== 전년 0, 올해 편성 {len(tops)}건 (가장 위 단계)')
    for r in tops:
        kids = [c for c in rows if c.get('parent') == r['id']]
        items = []
        stack = list(kids)
        while stack:
            c = stack.pop(0)
            if c['label'][:1] in '○':
                items.append(f"{c['label'].lstrip('○')} {c['vals'][0] if c['vals'] else ''}".strip())
            stack.extend(x for x in rows if x.get('parent') == c['id'])
        more = f" | 산출근거: {', '.join(items[:4])}{' 등' if len(items) > 4 else ''}" if items else ''
        print(f"   {r['vals'][0]:>12} | {' > '.join(r['path'][-2:] + [r['label']])} | p{r['page']}{more}")


def page_list_from_index(idx, rng, by_pdf):
    a, b = (rng.split('-') + [None])[:2]
    a, b = int(a), int(b) if b else int(a)
    meta = json.load(open(os.path.join(idx, 'meta.json'), encoding='utf-8'))
    def where(p):
        # 예전 색인은 build 를 실행한 폴더 기준 상대 경로: 색인 폴더의 위 폴더 기준으로도 찾아 봄
        if os.path.isabs(p) or os.path.exists(p):
            return p
        alt = os.path.join(os.path.dirname(os.path.abspath(idx.rstrip('/'))), p)
        return alt if os.path.exists(alt) else p
    files = {os.path.basename(s['file']): where(s['file']) for s in meta['sources']}
    out = {}
    with open(os.path.join(idx, 'pages.jsonl'), encoding='utf-8') as f:
        for line in f:
            r = json.loads(line)
            if r['kind'] != 'src':
                continue
            key = r['g'] if by_pdf else r['printed']
            if key is not None and a <= key <= b:
                path = files.get(r['file'])
                if path and path.lower().endswith('.pdf'):
                    out.setdefault(path, []).append((r['page'], r['printed'] if r['printed'] is not None else f"g{r['g']}"))
    return out


def spread_groups(pages):
    """두 쪽에 걸친 표 (왼쪽 쪽: 이름과 개요, 오른쪽 쪽: 숫자 열. 예: 예산서 계속비 사업조서).

    오른쪽 쪽의 숫자 줄을 기준 행으로 삼고, 같은 높이의 왼쪽 숫자와 위아래 사이의 왼쪽 글을
    왼쪽 머리글 열에 나눠 붙인다. '구분' 열 값(기존, 변경, 증감)이 처음 값으로 돌아오면 새 묶음.
    """
    groups = []
    for i in range(0, len(pages) - 1, 2):
        (pl, wl, L), (pr, wr, Rr) = pages[i], pages[i + 1]
        recs = [ln for ln in Rr if sum(1 for s in ln['segs'] if s['num']) >= 3]
        if not recs or not L:
            continue
        first = recs[0]['top']
        # 왼쪽 머리글: 첫 기준 행 위에서 글 조각이 가장 많은 줄
        lhead = max((ln for ln in L if ln['top'] < first - 1), key=lambda ln: len(ln['segs']), default=None)
        hcols = [((s['x0'] + s['x1']) / 2, s['text']) for s in lhead['segs']] if lhead else []
        # 오른쪽 머리글: 기준 행 위 글자를 가장 가까운 숫자 열에 붙이고, 두 줄 머리글(전년도 > 예산액, 지출액)은 묶음 이름을 나눠 줌
        rcols = [s['x1'] for s in recs[0]['segs'] if s['num']]
        centers = []
        for x1 in rcols:
            cs = [(s['x0'] + s['x1']) / 2 for ln in recs for s in ln['segs'] if s['num'] and abs(s['x1'] - x1) <= COL_TOL]
            centers.append(median(cs) if cs else x1 - 10)
        gap = min((b_ - a_ for a_, b_ in zip(centers, centers[1:])), default=60)
        words = [(ln['top'], (s['x0'] + s['x1']) / 2, s['text']) for ln in Rr if ln['top'] < first - 1 for s in ln['segs']]
        bottom = max((t for t, _, _ in words), default=0)
        upper = [[] for _ in rcols]
        lower = [[] for _ in rcols]
        for t, cx, txt in sorted(words):
            k = min(range(len(rcols)), key=lambda j: abs(centers[j] - cx))
            if abs(centers[k] - cx) > gap * 0.6:
                continue
            (lower if bottom - t <= 5 else upper)[k].append(txt)
        lend = [j for j in range(len(rcols)) if upper[j] and lower[j]]
        for k in range(len(rcols)):
            if lower[k] and not upper[k]:
                if lend:
                    j = min(lend, key=lambda j: (abs(j - k), j > k))
                    upper[k] = upper[j]
        rnames = [' '.join(upper[k] + lower[k]) or f'열{k + 1}' for k in range(len(rcols))]
        tops = [r['top'] for r in recs]
        head_bottom = lhead['top'] if lhead else first - 20
        page_recs = []
        for k, r in enumerate(recs):
            lo = (tops[k - 1] + tops[k]) / 2 if k else head_bottom + 1
            hi = (tops[k] + tops[k + 1]) / 2 if k + 1 < len(tops) else r['top'] + 15
            fields = {name: [] for _, name in hcols}
            left_vals = []
            for ln in L:
                if not (lo <= ln['top'] < hi) or (PRINTED_LINE.match(ln['text']) and ln['top'] > 0.85 * L[-1]['top']):
                    continue
                for s in ln['segs']:
                    if s['num'] and abs(ln['top'] - r['top']) <= 3 and s['x0'] > wl * 0.6:
                        left_vals.append(s['text'])
                        continue
                    if not hcols:
                        continue
                    cx = (s['x0'] + s['x1']) / 2
                    name = min(hcols, key=lambda h: abs(h[0] - cx))[1]
                    fields[name].append(s['text'])
            vals = []
            for x1 in rcols:
                seg = next((s for s in r['segs'] if s['num'] and abs(s['x1'] - x1) <= COL_TOL), None)
                vals.append(seg['text'] if seg else '')
            page_recs.append({'fields': {k_: ' '.join(v) for k_, v in fields.items() if v},
                              'left_vals': left_vals, 'vals': vals})
        # 묶기: '구분' 열(기존/변경/증감 같은 짧은 낱말)이 첫 값으로 돌아오면 새 묶음
        key_col = next((name for _, name in hcols if re.sub(r'\s', '', name) in ('구분', '구 분')), None)
        first_label = None
        cur = None
        for rec in page_recs:
            words = re.findall(r'[가-힣]+', rec['fields'].get(key_col, '')) if key_col else []
            if first_label is None:
                first_label = words[0] if words else ''
            if cur is None or (key_col and first_label and first_label in words):
                cur = {'pages': f'{pl}+{pr}', 'left_cols': [n for _, n in hcols], 'right_cols': rnames, 'records': []}
                groups.append(cur)
            cur['records'].append(rec)
    return groups


def print_spread(groups):
    last_cols = None
    for gi, g in enumerate(groups, 1):
        cols = (tuple(g['left_cols']), tuple(g['right_cols']))
        if cols != last_cols:
            print(f"# 왼쪽 열: {' | '.join(g['left_cols'])}")
            print(f"# 오른쪽 숫자 열: {' | '.join(g['right_cols'])}")
            last_cols = cols
        merged = {}
        for rec in g['records']:
            for k, v in rec['fields'].items():
                merged.setdefault(k, []).append(v)
        print(f"== 묶음 {gi} (p{g['pages']})")
        for k in g['left_cols']:
            if k in merged and not re.sub(r'\s', '', k) in ('구분',):
                print(f"   {k}: {' '.join(merged[k])}")
        for rec in g['records']:
            lab = next((v for k, v in rec['fields'].items() if re.sub(r'\s', '', k) == '구분'), '')
            m = re.search(r'(기존|변경|증감|당초|조정|계획|실적)\s*(\([A-Z\-]+\))?', lab)
            lab = m.group(0) if m else lab
            print(f"   [{lab}] {' '.join(rec['left_vals'])} | {' | '.join(rec['vals'])}")


def main():
    try:
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('idx', nargs='?', help='source_index.py 로 만든 색인 폴더')
    ap.add_argument('range', nargs='?', help='쪽 범위 (예: 834-860)')
    ap.add_argument('--file', help='색인 없이 PDF 하나 (쪽 범위는 PDF 쪽번호)')
    ap.add_argument('--pdf', action='store_true', help='색인의 전체 PDF 쪽번호로 범위 지정')
    ap.add_argument('--max-level', type=int)
    ap.add_argument('--find', nargs='+')
    ap.add_argument('--json')
    ap.add_argument('--no-head', action='store_true')
    ap.add_argument('--gone', action='store_true', help='전년 금액이 아래 단계 전년 합보다 큰 행 (없어진 하위 항목) 보기')
    ap.add_argument('--changes', action='store_true', help='증감액이 큰 행 (증액, 감액, 전년 0)')
    ap.add_argument('--new', action='store_true', help='전년 0, 올해 편성인 행 (가장 위 단계만)')
    ap.add_argument('--label', help='--changes, --new 에서 이름이 맞는 행만 (정규식. 예: "^[0-9]{3}" 은 편성목)')
    ap.add_argument('--min', type=float, default=0, help='--changes 의 최소 증감액 (표 단위, 예: 10000 = 1천만원)')
    ap.add_argument('--top', type=int, help='--changes 에서 종류별 위 몇 건')
    ap.add_argument('--spread', action='store_true', help='두 쪽에 걸친 표 (왼쪽 쪽 이름, 오른쪽 쪽 숫자. 예: 계속비 사업조서). 범위는 왼쪽 쪽부터')
    a = ap.parse_args()
    if a.file:
        if a.idx and not a.range:
            a.range = a.idx
        if not a.range:
            ap.error('쪽 범위가 필요합니다 (예: 3-9)')
        lo, hi = (a.range.split('-') + [None])[:2]
        lo, hi = int(lo), int(hi) if hi else int(lo)
        plan = {a.file: [(p, p) for p in range(lo, hi + 1)]}
    else:
        if not a.idx or not a.range:
            ap.error('색인 폴더와 쪽 범위 또는 --file 이 필요합니다')
        plan = page_list_from_index(a.idx, a.range, a.pdf)
        if not plan:
            sys.exit('그 범위의 PDF 쪽이 색인에 없습니다 (인쇄 쪽번호가 아니면 --pdf)')
    pages = []
    for path, pl in plan.items():
        pages += read_pages(path, pl)
    if a.spread:
        groups = spread_groups(pages)
        if a.json:
            json.dump(groups, open(a.json, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
            print(f'{len(groups)}묶음 → {a.json}', file=sys.stderr)
        if a.find:
            q = ' '.join(a.find).replace(' ', '')
            groups = [g for g in groups if any(q in ''.join(r['fields'].values()).replace(' ', '') for r in g['records'])]
        print_spread(groups)
        return
    rows, levels = build_rows(pages)
    check_identity(rows)
    if a.json:
        json.dump([dict({k: r[k] for k in ('id', 'parent', 'page', 'level', 'label', 'cols', 'vals', 'note', 'path')}, subs=r.get('subs', [])) for r in rows if not r['head']],
                  open(a.json, 'w', encoding='utf-8'), ensure_ascii=False, indent=0)
        print(f"{sum(1 for r in rows if not r['head'])}행 → {a.json}", file=sys.stderr)
    print('# 단계 위치: ' + ', '.join(f'L{i}={x:.0f}' for i, (x, _) in enumerate(levels)))
    if a.changes:
        print_changes(rows, a.label, a.min, a.top)
        return
    if a.new:
        print_new(rows, a.label)
        return
    if a.gone:
        g = gone_items(rows)
        print(f'# 전년 금액이 아래 단계 전년 합보다 큰 행 {len(g)}개: 차이는 올해 없어진 하위 항목의 전년 금액으로 보임')
        for p, d in g:
            print(f"{' > '.join(p['path'] + [p['label']])} | 전년 {p['vals'][1]} | 아래 합과 차이 {d:,.0f} (p{p['page']})")
        return
    if a.find:
        q = ' '.join(a.find).replace(' ', '')
        hits = [i for i, r in enumerate(rows) if q in r['label'].replace(' ', '')]
        for i in hits:
            r = rows[i]
            print('== ' + ' > '.join(r['path'] + [r['label']]))
            print(f"   열: {' | '.join(r['cols'])}")
            print('   ' + fmt_row(r))
            for c in rows[i + 1:]:
                if c['head']:
                    continue
                if c['level'] is None or r['level'] is None or c['level'] <= r['level']:
                    break
                if a.max_level is None or c['level'] <= a.max_level:
                    print('   ' + fmt_row(c))
        if not hits:
            print('찾은 행 없음')
        return
    last_cols, last_head = None, None
    heads = []
    for r in rows:
        if r['head']:
            heads.append(r)
            continue
        if heads:
            ht = ' / '.join(h['text'] for h in heads if HEAD_KV.match(h['text']))
            if ht and ht != last_head and not a.no_head:
                print(f"# p{heads[0]['page']} {ht}")
            last_head = ht
            heads = []
        if r['cols'] != last_cols:
            print(f"# p{r['page']} 열: {' | '.join(r['cols'])}")
            last_cols = r['cols']
        if a.max_level is not None and (r['level'] is None or r['level'] > a.max_level):
            continue
        print(fmt_row(r))


if __name__ == '__main__':
    main()
