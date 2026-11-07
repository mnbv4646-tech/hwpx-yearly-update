#!/usr/bin/env python3
"""작년 문서(양식) 분석: 블록, 표 종류, 칸 역할, 금액 문장.

  python3 analyze_template.py doc.hwpx [--year 2025] [--json map.json] [--block 3]

출력
  - 문서 기준 연도 (표 머리와 제목에서 자동 판별, --year 로 지정 가능)
  - 블록 목록: 제목 상자(1칸 표)로 나눈 구간, 단락 범위, 표, 금액 문장
  - 표마다 종류와 칸 역할
      compare : 올해/전년/증감(/증감률/쪽) 비교표     cols = {cur, prev, diff, rate, page, desc}
      list    : 사업 목록 (올해 금액, 설명, 쪽)        cols = {cur, desc, page}
      multi   : 연도별 열이 여러 개 있는 표 (계속비 등)
      factor  : 요인, 내용 같은 글 위주의 표
      title   : 제목 상자 (1칸)
      other   : 그 밖
  - 단위: 표 안이나 바로 앞 단락의 '(단위 : 천원)'
"""
import argparse
import signal
import json
import os
import re
import sys

sys.dont_write_bytecode = True     # skill 폴더에 __pycache__ 를 만들지 않음
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hwpx_ops as H  # noqa: E402
import korean_amount as K  # noqa: E402

YEAR_RE = re.compile(r'(?<![\d,])(20\d\d)(?![\d,])')
UNIT_RE = re.compile(r'단위\s*[:：]?\s*(천원|백만원|원|만원|억원)')


def norm(s):
    return re.sub(r'\s+', '', s or '')


def detect_year(doc):
    """표 머리의 연도와 제목 상자의 'NNNN년도' 중 가장 큰 기준 연도."""
    c = Counter()
    for t in doc.tables:
        rows = H.tbl_rows(t)
        if int(t.get('rowCnt')) * int(t.get('colCnt')) == 1:
            txt = H.cell_text(next(t.iter(H.HP + 'tc')))
            for y in re.findall(r'(20\d\d)\s*년도', txt):
                c[int(y)] += 3
            continue
        for tr in rows[:3]:
            for tc in tr:
                if H.local(tc) != 'tc':
                    continue
                txt = norm(H.cell_text(tc))
                for y in YEAR_RE.findall(txt):
                    if re.search(r'(본예산|예산|요구|당초|결산|계획|실적)', txt):
                        c[int(y)] += 1
    if not c:
        return None
    top = max(c.values())
    return max(y for y, v in c.items() if v >= top * 0.3)


def _is_data_row(texts):
    for t in texts:
        x = re.sub(r'(19|20)\d\d', '', t)          # 연도는 머리글에도 나옴
        x = re.sub(r'\([A-Za-z가-힣\-+]+\)', '', x)    # (A), (B-A)
        x = re.sub(r'단위[^)]*', '', x)
        if re.search(r'\d', x):
            return True
    return False


def header_row_index(rows):
    """첫 데이터 행(연도 말고 숫자가 있는 행) 바로 위 행. '(단위 : 천원)' 한 칸 행은 건너뜀."""
    hi = None
    for ri, tr in enumerate(rows[:5]):
        tcs = [tc for tc in tr if H.local(tc) == 'tc']
        texts = [H.cell_text(tc) for tc in tcs]
        if len(tcs) == 1 and UNIT_RE.search(texts[0]) and len(rows) > 1:
            continue
        if _is_data_row(texts):
            break
        hi = ri
    return hi


def col_roles(names, year):
    roles = {}

    def first(pred):
        for c, n in names.items():
            if pred(n):
                return c
        return None
    y, py = str(year), str(year - 1) if year else None
    roles['cur'] = first(lambda n: (y and y in n and not re.search(r'이후|년이후', n)) or n in ('예산액', '금회', '올해', '금년도'))
    roles['prev'] = first(lambda n: (py and py in n) or '기정' in n or '전년' in n)
    roles['diff'] = first(lambda n: n.startswith('증감') and '률' not in n or n in ('비교증감', '증감(A-B)', '증감'))
    roles['rate'] = first(lambda n: '률' in n or n == '%' or '비율' in n)
    roles['page'] = first(lambda n: '페이지' in n or n in ('쪽', '면'))
    roles['desc'] = first(lambda n: re.search(r'(개요|사유|주요내용|내용|요인|비고)', n) is not None)
    roles['label'] = first(lambda n: re.search(r'(구분|사업명|과목|부서|명칭)', n) is not None)
    return {k: v for k, v in roles.items() if v is not None}


def classify_table(t, year):
    rc, cc = int(t.get('rowCnt')), int(t.get('colCnt'))
    rows = H.tbl_rows(t)
    if rc * cc == 1:
        return {'kind': 'title', 'text': H.cell_text(next(t.iter(H.HP + 'tc')))}
    hi = header_row_index(rows)
    unit = None
    for tr in rows[:2]:
        for tc in tr:
            if H.local(tc) == 'tc':
                m = UNIT_RE.search(H.cell_text(tc))
                if m:
                    unit = m.group(1)
    info = {'rows': rc, 'cols': cc, 'header_row': hi, 'unit': unit}
    if hi is None:
        info['kind'] = 'other'
        return info
    names = {}
    for tc in rows[hi]:
        if H.local(tc) == 'tc':
            r, c, rs, cs = H.cell_info(tc)
            names[c] = norm(H.cell_text(tc))
    # 두 줄 머리: 위 행 이름을 합침
    if hi > 0:
        for tc in rows[hi - 1]:
            if H.local(tc) == 'tc':
                r, c, rs, cs = H.cell_info(tc)
                if rs > 1 and c not in names:
                    names[c] = norm(H.cell_text(tc))
    roles = col_roles(names, year)
    info['header'] = names
    info['roles'] = roles
    joined = '|'.join(names.values())
    years_in_header = set(YEAR_RE.findall(joined))
    if '총사업비' in joined or len(years_in_header) >= 3:
        info['kind'] = 'multi'
    elif 'cur' in roles and 'prev' in roles and 'diff' in roles:
        info['kind'] = 'compare'
    elif 'cur' in roles and ('desc' in roles or 'page' in roles):
        info['kind'] = 'list'
    elif re.search(r'(요인|내용|사유)', joined):
        info['kind'] = 'factor'
    else:
        info['kind'] = 'other'
    info['data_start'] = hi + 1
    labels = []
    for tr in rows[hi + 1:]:
        tcs = [tc for tc in tr if H.local(tc) == 'tc']
        if tcs:
            labels.append(H.cell_text(tcs[0]).replace('\n', ' ')[:30])
    info['row_labels'] = labels
    return info


SENT_RE = re.compile(r'원\s*(으로|이며|이고)\s.*?(보다|대비)\s*[\d,.]+\s*%\s*(증가|감소)한')
SENT_NOCHANGE_RE = re.compile(r'(예산|예산액|예산은)\S*\s.*(보다|대비)\s*변동\s*없')


def is_amount_sentence(txt):
    """'~원으로 ~보다 X% 증가한 ~' 형태의 총괄 금액 문장. 사업별 서술 문장은 제외."""
    if SENT_NOCHANGE_RE.search(txt):
        return True
    return bool(SENT_RE.search(txt)) and len(K.find_amounts(txt)) >= 1


def split_blocks(doc, pattern=None):
    """제목 상자(1칸 표)가 연달아 나오는 묶음마다 블록 시작. pattern 을 주면 그 글자를 담은 제목 상자만."""
    pat = re.compile(pattern) if pattern else None
    paras = list(doc.top_paras())
    starts = []
    prev_title_i = None
    for k, (si, i, p) in enumerate(paras):
        titles = [t for t in p.iter(H.HP + 'tbl') if H.holder_para(t) is p and int(t.get('rowCnt')) * int(t.get('colCnt')) == 1]
        if not titles:
            if H.para_text(p).strip():
                prev_title_i = None
            continue
        txt = ' '.join(H.cell_text(next(t.iter(H.HP + 'tc'))) for t in titles)
        if pat and not pat.search(txt):
            continue
        if prev_title_i is not None and k - prev_title_i <= 3:
            starts[-1]['titles'].append(txt.strip())
        else:
            starts.append({'k': k, 'titles': [txt.strip()]})
        prev_title_i = k
    blocks = []
    for bi, st in enumerate(starts):
        k0 = st['k']
        k1 = starts[bi + 1]['k'] - 1 if bi + 1 < len(starts) else len(paras) - 1
        if bi == 0 and k0 > 0:
            blocks.append({'name': '(앞부분)', 'titles': [], 'k0': 0, 'k1': k0 - 1})
        blocks.append({'name': re.sub(r'\s+', ' ', st['titles'][-1]), 'titles': st['titles'], 'k0': k0, 'k1': k1})
    if not blocks:
        blocks = [{'name': '(전체)', 'titles': [], 'k0': 0, 'k1': len(paras) - 1}]
    for b in blocks:
        a = paras[b['k0']]
        z = paras[b['k1']]
        b['range'] = f's{a[0]}:p{a[1]}-s{z[0]}:p{z[1]}'
    return blocks, paras


def analyze(path, year=None, pattern=None):
    doc = H.HwpxDoc(path)
    year = year or detect_year(doc)
    blocks, paras = split_blocks(doc, pattern)
    out = {'file': path, 'year': year, 'blocks': []}
    for b in blocks:
        info = {'name': b['name'], 'titles': b['titles'], 'range': b['range'], 'tables': [], 'sentences': [],
                'numeric_paragraphs': []}
        unit_ctx = None
        for k in range(b['k0'], b['k1'] + 1):
            si, i, p = paras[k]
            txt = H.para_text(p).strip()
            m = UNIT_RE.search(txt)
            if m:
                unit_ctx = m.group(1)
            for t in p.iter(H.HP + 'tbl'):
                ti = classify_table(t, year)
                ti['id'] = doc.tid_of(t)
                ti['para'] = f's{si}:p{i}'
                if ti.get('unit') is None and ti['kind'] not in ('title',):
                    ti['unit'] = unit_ctx
                info['tables'].append(ti)
            for q in p.iter(H.HP + 'p'):
                if q.find('.//' + H.HP + 'p') is not None:
                    continue
                qt = H.para_text(q).strip()
                if not qt:
                    continue
                where = f's{si}:p{i}' if q is p else f'{doc.tid_of(next(q.iterancestors(H.HP + "tbl")))}' if any(True for _ in q.iterancestors(H.HP + 'tbl')) else f's{si}:p{i}'
                if is_amount_sentence(qt):
                    info['sentences'].append({'para': where, 'text': qt})
                elif q is p and re.search(r'\d', qt) and K.find_amounts(qt):
                    info['numeric_paragraphs'].append({'para': where, 'text': qt[:120]})
        info['warnings'] = sentence_warnings(info['sentences'])
        out['blocks'].append(info)
    return out


def sentence_warnings(sents):
    """작년 문서 금액 문장의 이상한 곳: 줄바꿈과 끊긴 금액, 같은 금액 문장의 반복."""
    warns = []
    for s in sents:
        t = s['text']
        head, _, tail = t.partition('\n')
        if tail.strip() and re.search(r'(되었|하였|었|였)음\.?\s*$', head):
            warns.append(f'{s["para"]}: 끝난 문장 뒤 줄바꿈에 다른 글 조각이 붙어 있음 | ...{head[-12:]}⏎{tail[:30]} (set_para 로 다시 쓰기)')
        elif re.search(r'\d+\s*억(\s*\d+\s*천)?(\s*\d+\s*백)?\d?\s*$', t):
            warns.append(f'{s["para"]}: 금액이 끝에서 끊김 | {t[-40:]}')
    first = Counter()
    where = {}
    for s in sents:
        amts = K.find_amounts(s['text'])
        if amts:
            key = s['text'][amts[0][0]:amts[0][1]].replace(' ', '')
            first[key] += 1
            where.setdefault(key, []).append(s['para'])
    for key, n in first.items():
        if n >= 4:
            warns.append(f'같은 첫 금액({key}) 문장이 {n}번: {", ".join(where[key][:6])} (부서별 문장이면 각 부서 값으로 써야 하는지 확인)')
    return warns


def summary(res, only=None):
    print(f'기준 연도: {res["year"]}  (새 문서는 {res["year"] + 1 if res["year"] else "?"})')
    print(f'블록 {len(res["blocks"])}개')
    for bi, b in enumerate(res['blocks']):
        if only is not None and bi != only:
            continue
        kinds = Counter(t['kind'] for t in b['tables'])
        print(f'\n[블록 {bi}] {b["name"]}  {b["range"]}  표 {dict(kinds)}  금액문장 {len(b["sentences"])}')
        for w in b.get('warnings', []):
            print(f'  ! {w}')
        if only is None:
            continue
        for t in b['tables']:
            if t['kind'] == 'title':
                continue
            roles = ' '.join(f'{k}=c{v}' for k, v in t.get('roles', {}).items())
            print(f'  {t["id"]} ({t["para"]}) {t["kind"]} {t["rows"]}x{t["cols"]} 단위={t.get("unit")} 머리행=r{t.get("header_row")} {roles}')
            if t.get('row_labels'):
                print('      행: ' + ' / '.join(t['row_labels'][:12]) + (' …' if len(t['row_labels']) > 12 else ''))
        for s in b['sentences']:
            print(f'  문장 {s["para"]}: {s["text"][:110]}')
        for s in b['numeric_paragraphs'][:15]:
            print(f'  금액 서술 {s["para"]}: {s["text"]}')


def main():
    try:
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('doc')
    ap.add_argument('--year', type=int)
    ap.add_argument('--pattern', help='블록 시작으로 볼 제목 상자의 정규식 (기본: 모든 제목 상자)')
    ap.add_argument('--json')
    ap.add_argument('--block', type=int, help='이 블록만 자세히')
    a = ap.parse_args()
    res = analyze(a.doc, a.year, a.pattern)
    if a.json:
        json.dump(res, open(a.json, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    summary(res, a.block)


if __name__ == '__main__':
    main()
