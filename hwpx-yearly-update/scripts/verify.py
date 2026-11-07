#!/usr/bin/env python3
"""완성 문서 자가 검증.

  python3 verify.py --new 올해.hwpx --old 작년.hwpx --index IDX [--rate-mode round] [--block 3] [--year 2026] [--json out.json]

정답 없이 확인할 수 있는 것만 본다.
  [오류] 패키지 정합성 (한글에서 '손상'으로 열리는 원인)
  [오류] 작년에만 있던 숫자가 남음 (원자료에 없고 작년 문서에만 있던 값)
  [오류] 증감액, 증감률 계산 불일치 / 금액 문장과 표 불일치 / 증가와 감소 방향 반대
  [확인] '증액' 제목 표에 감소 행, '감액' 제목 표에 증가 행
  [확인] 원자료에서 찾을 수 없는 숫자 / 적힌 쪽에 그 금액이 없음 / 합계 행 불일치
  [확인] 지난 연도 표기, 빈 표, 미완성 문장, 자리표시(○○ 등)
  [오류] 작년 표 관례인 재원 줄("시 1,000")이 새 행 금액 칸에 없음
  [확인] 작년 서술에 있던 굵게가 없음, 줄 앞 들여쓰기가 작년과 다름, 칸 첫 줄이 빈 줄
  [확인] 칸 안에 작년에 없던 소제목(판단 근거 등), 작년 칸에 늘 있던 항목(위치, 기간 등)이 빠짐
  [정보] [검토 필요] 표시 위치
"""
import argparse
import signal
import json
import os
import re
import sys

sys.dont_write_bytecode = True     # skill 폴더에 __pycache__ 를 만들지 않음
from collections import defaultdict
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hwpx_ops as H  # noqa: E402
import korean_amount as K  # noqa: E402
import analyze_template as A  # noqa: E402
import check_inputs as C  # noqa: E402

NUM_RE = re.compile(r'(?<![\d,.])(?:△?\d{1,3}(?:,\d{3})+(?:\.\d+)?|△?\d+\.\d+|△?\d{4,})(?![\d,]*\s*[조억천백만])')


def nrm(tok):
    return tok.replace('△', '').replace(',', '')


def is_year(tok):
    return re.fullmatch(r'(19|20)\d\d', tok) is not None


def tokens(text):
    return [t for t in NUM_RE.findall(text) if not is_year(t)]


def doc_text(doc):
    out = []
    for root in doc.sections:
        for t in root.iter(H.HP + 't'):
            out.append(t.text or '')
            for ch in t:
                out.append(ch.tail or '')
    return ' '.join(out)


PLAIN = {}      # 인쇄 쪽 -> 뒤에 '원'이 붙지 않은 숫자 (천원 표 값과 대조용. 산출식의 '270,000원' 같은 단가 제외)
SRC_PLAIN = set()


def load_sources(idx):
    src_tok, aux_tok, printed = set(), set(), {}
    with open(os.path.join(idx, 'pages.jsonl'), encoding='utf-8') as f:
        for line in f:
            r = json.loads(line)
            toks = {nrm(t) for t in NUM_RE.findall(r['text'])}
            if r['kind'] == 'src':
                plain = {nrm(m.group(0)) for m in NUM_RE.finditer(r['text']) if not re.match(r'\s*원', r['text'][m.end():m.end() + 3])}
                SRC_PLAIN.update(plain)
                if r['printed'] is not None:
                    PLAIN.setdefault(r['printed'], set()).update(plain)
                src_tok |= toks
                if r['printed'] is not None:
                    # 쪽번호 대조용: 세 자리 이하 숫자(400 등)도 넣는다
                    short = {nrm(t) for t in re.findall(r'(?<![\d,.])△?\d{1,3}(?![\d,.])', r['text'])}
                    printed.setdefault(r['printed'], set()).update(toks | short)
            else:
                aux_tok |= toks
    return src_tok, aux_tok, printed


def cell_lines_numbers(text):
    """칸의 첫 숫자를 맨 앞에 두고, 그 뒤는 재원 이름이 붙은 줄의 숫자 (총액, {재원: 값})."""
    first = None
    funds = {}
    for line in text.split('\n'):
        m = re.search(r'△?\d[\d,]*(?:\.\d+)?', line)
        if not m:
            continue
        lab = re.sub(r'[\s:：]', '', line[:m.start()])
        if first is None and not lab:
            first = m.group(0)
        elif lab and len(lab) <= 4:
            funds[lab] = m.group(0)
        elif first is None:
            first = m.group(0)
    return [first] + [None] if first else [], funds


def first_line_blank(text):
    lines = text.split('\n')
    return len(lines) > 1 and not lines[0].strip() and any(re.search(r'\d', l) for l in lines[1:])


def heading_before(tbl, maxback=3):
    """표 바로 위의 제목 글 (표를 담은 단락의 글 + 그 앞 글 단락 몇 개)."""
    top = H.containing_top_para(tbl)
    if top is None:
        return ''
    own = H.para_text(top).strip()
    if own and len(own) <= 40:
        return own
    sib, n = top.getprevious(), 0
    while sib is not None and n < maxback:
        if H.local(sib) == 'p':
            if sib.find('.//' + H.HP + 'tbl') is not None:
                break
            t = H.para_text(sib).strip()
            if t:
                # 제목처럼 짧은 단락만 (금액 문장이 앞에 있으면 제목이 아님)
                return t if len(t) <= 40 and not re.search(r'(음|다)\.?$', t) else ''
            n += 1
        sib = sib.getprevious()
    return ''


TOTAL_WORDS = re.compile(r'^(합계|소계|계|총계|총괄|누계)$')


def cell_context(q):
    """단락 q 가 표 칸 안에 있으면 (그 행의 이름 칸 글자, 그 열의 머리 글자), 아니면 None."""
    tc = next(q.iterancestors(H.HP + 'tc'), None)
    if tc is None:
        return None
    tbl = next(tc.iterancestors(H.HP + 'tbl'))
    r, c, _, _ = H.cell_info(tc)
    label, head = [], []
    for tr in H.tbl_rows(tbl):
        for x in tr:
            if H.local(x) != 'tc':
                continue
            xr, xc, rs, cs = H.cell_info(x)
            t = H.cell_text(x).strip()
            # 같은 행을 덮는 왼쪽 칸 (병합 포함) 중 숫자가 아닌 글자
            if xc < c and xr <= r < xr + rs and t and not re.fullmatch(r'[△\d,.\s%-]+', t):
                label.append(t)
            # 같은 열 위쪽의 머리 글자
            if xr < r and xc <= c < xc + cs and t and not re.search(r'\d{2,}', t.replace('20', '', 1)):
                head.append(t)
    return label, ' '.join(head)


def _first_num(text):
    m = re.search(r'△?\d[\d,]*', text or '')
    return to_int(m.group(0)) if m else None


def total_matches(q, tok):
    """합계 행의 값이 같은 열 다른 행(합계가 아닌 행)의 합과 같은지. 같은 '구분'(기존, 변경 등)끼리 더한다."""
    tc = next(q.iterancestors(H.HP + 'tc'), None)
    if tc is None:
        return False
    tbl = next(tc.iterancestors(H.HP + 'tbl'))
    r0, c0, _, _ = H.cell_info(tc)
    cells = [(H.cell_info(x), H.cell_text(x).strip()) for tr in H.tbl_rows(tbl) for x in tr if H.local(x) == 'tc']

    def labels_of(r, c):
        out = []
        for (xr, xc, rs, cs), t in cells:
            if xc < c and xr <= r < xr + rs and t and not re.fullmatch(r'[△\d,.\s%-]+', t):
                out.append((xc, re.sub(r'\s', '', t)))
        return [t for _, t in sorted(out)]
    target_labels = labels_of(r0, c0)
    sub = target_labels[-1] if target_labels and not TOTAL_WORDS.match(target_labels[-1]) else None
    total = 0
    n = 0
    for (xr, xc, rs, cs), t in cells:
        if xc != c0 or xr == r0:
            continue
        ls = labels_of(xr, c0)
        if not ls or any(TOTAL_WORDS.match(w) for w in ls):
            continue
        if sub is not None and ls[-1] != sub:
            continue
        v = _first_num(t)
        if v is not None:
            total += v
            n += 1
    try:
        return n > 0 and total == to_int(tok)
    except Exception:  # noqa: BLE001
        return False


def derived_value(q, tok):
    """합계 행이나 비율 칸처럼 계산으로 만든 값인지 (작년 숫자와 우연히 같을 수 있음)."""
    ctx = cell_context(q)
    if ctx is None:
        return '.' in tok and re.search(re.escape(tok) + r'\s*%', H.para_text(q)) is not None
    labels, head = ctx
    if any(TOTAL_WORDS.match(re.sub(r'\s', '', w)) for w in labels):
        return total_matches(q, tok)      # 아래 행 합과 같아야 계산값. 다르면 작년 합계가 남은 것
    return '.' in tok and bool(re.search(r'률|율|%', head))


def direction_of(heading):
    up = re.search(r'증액|증가', heading)
    down = re.search(r'감액|감소', heading)
    if up and not down:
        return 1
    if down and not up:
        return -1
    return 0


def to_int(tok):
    return int(Decimal(nrm(tok))) * (-1 if tok.startswith('△') else 1)


class Report:
    def __init__(self):
        self.items = []

    def add(self, level, kind, where, msg):
        self.items.append({'level': level, 'kind': kind, 'where': where, 'msg': msg})

    def print(self, limit=40):
        order = {'오류': 0, '확인': 1, '정보': 2}
        groups = defaultdict(list)
        for it in self.items:
            groups[(it['level'], it['kind'])].append(it)
        cnt = defaultdict(int)
        for (lv, kd), lst in groups.items():
            cnt[lv] += len(lst)
        print(f'요약: 오류 {cnt["오류"]}건, 확인 {cnt["확인"]}건, 정보 {cnt["정보"]}건')
        for (lv, kd) in sorted(groups, key=lambda x: (order[x[0]], x[1])):
            lst = groups[(lv, kd)]
            print(f'\n[{lv}] {kd} ({len(lst)}건)')
            for it in lst[:limit]:
                print(f'  - {it["where"]}: {it["msg"]}')
            if len(lst) > limit:
                print(f'  ... 외 {len(lst) - limit}건')


def check_package(path, rep):
    res = C.hwpx_health(path)
    for p in res.get('problems', []):
        rep.add('오류', '패키지', os.path.basename(path), p)



FUND_LINE = re.compile(r'^\s*[가-힣]{1,2}\s*△?\s*[\d,]+\s*$')
LEADER = re.compile(r'^(\s*)([-○◦∘•▸▶▷※ㆍ])')


def _block_key(name):
    return re.sub(r'\s|\(.*?\)', '', name or '')


def _header_key(ti):
    return '|'.join(re.sub(r'20\d\d', 'Y', v) for _, v in sorted((ti.get('header') or {}).items(), key=lambda kv: int(kv[0])))


def _data_cells(doc, ti, role):
    c = (ti.get('roles') or {}).get(role)
    if c is None:
        return []
    out = []
    for tr in H.tbl_rows(doc.table(ti['id']))[(ti.get('data_start') or 0):]:
        for tc in tr:
            if H.local(tc) == 'tc' and H.cell_info(tc)[1] == c:
                out.append(tc)
    return out


def check_conventions(doc, old_doc, res, old_res, rep, in_scope):
    """작년 문서의 양식 관례를 새 문서가 지키는지: 금액 칸 재원 줄, 서술의 굵게, 줄 앞 들여쓰기, 칸 첫 줄 빈 줄."""
    bids_new, bids_old = H.bold_ids(doc), H.bold_ids(old_doc)
    old_blocks = {_block_key(b['name']): b for b in old_res['blocks']}
    for b in res['blocks']:
        ob = old_blocks.get(_block_key(b['name']))
        if ob is None:
            continue
        # 1) 재원 줄: 작년 같은 꼴 표의 금액 칸 대부분에 재원 줄이 있으면 새 행에도 있어야 함
        old_tabs = {}
        for ti in ob['tables']:
            old_tabs.setdefault((ti['kind'], _header_key(ti)), []).append(ti)
        for ti in b['tables']:
            cand = old_tabs.get((ti['kind'], _header_key(ti)))
            if not cand or ti['kind'] not in ('compare', 'list'):
                continue
            tbl = doc.table(ti['id'])
            if not in_scope(H.containing_top_para(tbl)):
                continue
            for role in ('cur', 'prev', 'diff'):
              ocells = [tc for oti in cand for tc in _data_cells(old_doc, oti, role) if H.cell_text(tc).strip()]
              if not ocells:
                  continue
              with_fund = sum(1 for tc in ocells if any(FUND_LINE.match(x) for x in H.cell_text(tc).split('\n')[1:]))
              if with_fund * 2 < len(ocells):
                  continue
              for tc in _data_cells(doc, ti, role):
                lines = H.cell_text(tc).split('\n')
                if lines and re.match(r'^\s*△?\s*[\d,]*\d\s*$', lines[0]) and not any(FUND_LINE.match(x) for x in lines[1:]):
                    r, c = H.cell_info(tc)[:2]
                    rep.add('오류', '재원 줄 누락', f"{ti['id']}:r{r}c{c}",
                            f'작년 같은 표는 이 열 금액 칸에 재원 줄(예: "시 1,000")이 있는데 이 칸에는 없음. 예산서에 재원 줄이 없는 일반회계 항목이면 전액 시비 | {H.short(H.cell_text(tc), 40)}')
        # 1b) 칸 안 구조: 작년 같은 열 칸의 소제목(∘ 사업개요)과 항목 이름(- 위    치 :)
        SUB = re.compile(r'^\s*[∘◦•ㆍ]\s*(\S.*?)\s*$')
        LAB = re.compile(r'^\s*-\s*([가-힣][가-힣 ]{0,10}?)\s*:')

        def cell_struct(tc):
            subs, labs = set(), set()
            for x in H.cell_text(tc).split('\n'):
                m = SUB.match(x)
                if m:
                    subs.add(re.sub(r'\s', '', m.group(1)))
                m = LAB.match(x)
                if m:
                    labs.add(re.sub(r'\s', '', m.group(1)))
            return subs, labs
        for ti in b['tables']:
            cand = old_tabs.get((ti['kind'], _header_key(ti)))
            if not cand:
                continue
            tbl = doc.table(ti['id'])
            if not in_scope(H.containing_top_para(tbl)):
                continue
            col_old = {}
            for oti in cand:
                for tr in H.tbl_rows(old_doc.table(oti['id']))[(oti.get('data_start') or 0):]:
                    for tc in tr:
                        if H.local(tc) == 'tc':
                            st = cell_struct(tc)
                            if st[0]:
                                col_old.setdefault(H.cell_info(tc)[1], []).append(st)
            for tr in H.tbl_rows(tbl)[(ti.get('data_start') or 0):]:
                for tc in tr:
                    if H.local(tc) != 'tc':
                        continue
                    r, c = H.cell_info(tc)[:2]
                    olds_c = col_old.get(c)
                    if not olds_c:
                        continue
                    subs, labs = cell_struct(tc)
                    all_subs = set().union(*(o[0] for o in olds_c))
                    extra = sorted(subs - all_subs)
                    if extra:
                        rep.add('확인', '작년에 없던 칸 소제목', f"{ti['id']}:r{r}c{c}",
                                f"{', '.join(extra)}: 판단 근거나 메모는 문서가 아니라 최종 보고에 쓴다")
                    common = set.intersection(*(o[1] for o in olds_c)) if olds_c else set()
                    miss = sorted(common - labs)
                    if miss:
                        rep.add('확인', '작년 칸 항목 빠짐', f"{ti['id']}:r{r}c{c}",
                                f"{', '.join(miss)}: 줄을 지우지 말고 '- 이름 : [검토 필요]'로 남긴다")
        # 2) 서술의 굵게와 3) 줄 앞 들여쓰기
        def top_paras(d, rng):
            return [p for p in d.scope_elements(rng) if H.local(p) == 'p' and p.find('.//' + H.HP + 'tbl') is None]
        olds, news = top_paras(old_doc, ob['range']), [p for p in top_paras(doc, b['range']) if in_scope(p)]
        def bold_count(ps, bids):
            return sum(1 for p in ps for r in H.own_runs(p) if r.get('charPrIDRef') in bids and H._run_text(r).strip())
        if bold_count(olds, bids_old) and not bold_count(news, bids_new):
            rep.add('확인', '굵게 빠짐', b['name'], '작년 문서는 서술(검토의견 금액, 사업명 등)에 굵게를 썼는데 새 문서에는 굵은 글자가 없음. set_para 와 insert_para 글에 **굵게** 표시를 쓴다')
        from collections import Counter
        lead = {}
        for p in olds:
            m = LEADER.match(H.para_text(p))
            if m:
                lead.setdefault(m.group(2), Counter())[m.group(1)] += 1
        for p in news:
            t = H.para_text(p)
            m = LEADER.match(t)
            if not m or m.group(2) not in lead:
                continue
            common, n = lead[m.group(2)].most_common(1)[0]
            if n * 5 >= sum(lead[m.group(2)].values()) * 4 and m.group(1) != common:
                rep.add('확인', '들여쓰기 다름', b['name'], f'작년 "{m.group(2)}" 줄은 앞 빈칸 {len(common)}칸인데 이 줄은 {len(m.group(1))}칸 | {H.short(t, 60)}')
        # 4) 칸 첫 줄이 빈 줄: 작년 같은 꼴 표의 같은 열에서 그런 칸이 없을 때만
        def blank_first(d, tc):
            lines = H.cell_text(tc).split('\n')
            return len(lines) > 1 and not lines[0].strip() and any(x.strip() for x in lines[1:])
        old_blank_cols = set()
        for oti in ob['tables']:
            for tr in H.tbl_rows(old_doc.table(oti['id']))[(oti.get('data_start') or 0):]:
                for tc in tr:
                    if H.local(tc) == 'tc' and blank_first(old_doc, tc):
                        old_blank_cols.add((oti['kind'], _header_key(oti), H.cell_info(tc)[1]))
        for ti in b['tables']:
            tbl = doc.table(ti['id'])
            for tr in H.tbl_rows(tbl)[(ti.get('data_start') or 0):]:
                for tc in tr:
                    if H.local(tc) != 'tc':
                        continue
                    if blank_first(doc, tc) and (ti['kind'], _header_key(ti), H.cell_info(tc)[1]) not in old_blank_cols:
                        r, c = H.cell_info(tc)[:2]
                        rep.add('확인', '칸 첫 줄 빈 줄', f"{ti['id']}:r{r}c{c}", H.short(H.cell_text(tc).replace('\n', ' / '), 50))


def verify(new, old=None, idx=None, rate_mode='round', only_block=None, rep=None, year=None):
    rep = rep or Report()
    check_package(new, rep)
    doc = H.HwpxDoc(new)
    old_doc = H.HwpxDoc(old) if old else None
    detected = A.detect_year(doc)
    old_year = A.detect_year(old_doc) if old_doc else None
    # 기준 연도: --year > 작년 문서 + 1 > 새 문서에서 찾은 연도.
    # 블록 몇 개만 고친 중간본은 작년 연도가 더 많아 새 문서만으로는 잘못 잡힌다.
    year = year or (old_year + 1 if old_year else detected)
    if old_year is None and year:
        old_year = year - 1
    res = A.analyze(new, year)
    src_tok, aux_tok, printed = (load_sources(idx) if idx else (None, set(), {}))
    new_text = doc_text(doc)
    old_tok = {nrm(t) for t in tokens(doc_text(old_doc))} if old_doc else set()
    allowed = None
    if only_block is not None:
        rng = res['blocks'][only_block]['range']
        allowed_els = doc.scope_elements(rng)      # 참조를 붙잡아 두어야 id가 안정적 (lxml 프록시)
        allowed = {id(p) for p in allowed_els}

    def in_scope(p):
        return allowed is None or id(p) in allowed

    # 1) 작년에만 있던 숫자
    if old_doc and src_tok is not None:
        # 작년 문서의 '올해' 열 값은 올해 문서의 '전년' 열로 정당하게 넘어온다
        carry = set()
        for b in A.analyze(old, old_year)['blocks']:
            for ti in b['tables']:
                if ti['kind'] not in ('compare', 'list'):
                    continue
                cur_c = ti.get('roles', {}).get('cur')
                tbl = old_doc.table(ti['id'])
                for tr in H.tbl_rows(tbl)[(ti.get('data_start') or 0):]:
                    for tc in tr:
                        if H.local(tc) == 'tc' and H.cell_info(tc)[1] == cur_c:
                            carry |= {nrm(t) for t in tokens(H.cell_text(tc))}
        old_only = {t for t in old_tok if t not in src_tok and t not in aux_tok and t not in carry and len(t.replace('.', '')) >= 4}
        seen = set()
        for si, i, p in doc.top_paras():
            if not in_scope(p):
                continue
            for q in p.iter(H.HP + 'p'):
                if q.find('.//' + H.HP + 'p') is not None:
                    continue
                txt = H.para_text(q)
                for t in tokens(txt):
                    key = nrm(t)
                    if key in old_only and (key, f's{si}:p{i}') not in seen:
                        seen.add((key, f's{si}:p{i}'))
                        where = f's{si}:p{i}'
                        tb = next(q.iterancestors(H.HP + 'tbl'), None)
                        if tb is not None:
                            where = doc.tid_of(tb) or where
                        if derived_value(q, t):
                            if '.' in t:
                                rep.add('확인', '작년 숫자와 같은 계산값', where, f'{t} (증감률 칸의 값이 작년 문서의 숫자와 같음. 계산이 맞으면 정상) | {H.short(txt, 80)}')
                            # 합계 행이고 아래 행 합과 같으면 올바른 계산값이라 알리지 않음
                        else:
                            rep.add('오류', '작년 숫자 잔재', where, f'{t} (원자료에 없고 작년 문서에 있던 값) | {H.short(txt, 80)}')

    # 금액 문장 대조용: 문서 전체 비교표 값과 원자료 숫자 (천원 기준으로 원 단위 환산)
    import bisect
    pool = set()
    for b_ in res['blocks']:
        for ti in b_['tables']:
            if ti['kind'] != 'compare':
                continue
            unit_won = K.UNIT_WON.get(ti.get('unit') or '천원', 1000)
            for t in tokens(' '.join(H.cell_text(tc) for tc in doc.table(ti['id']).iter(H.HP + 'tc'))):
                try:
                    pool.add(abs(to_int(t)) * unit_won)
                except Exception:  # noqa: BLE001
                    pass
    for t in (src_tok or ()):
        if '.' not in t:
            pool.add(int(t) * 1000)
    pool = sorted(pool)

    def near_values(won):
        i = bisect.bisect_left(pool, won - 500_000)
        j = bisect.bisect_right(pool, won + 500_000)
        return pool[i:j]

    # 2) 블록별 표 검사
    for bi, b in enumerate(res['blocks']):
        if only_block is not None and bi != only_block:
            continue
        pairs = []          # (올해, 전년) 후보 - 금액 문장 대조용
        for ti in b['tables']:
            if ti['kind'] in ('title',):
                continue
            tbl = doc.table(ti['id'])
            rows = H.tbl_rows(tbl)
            ro = ti.get('roles', {})
            data = rows[(ti.get('data_start') or 0):]
            # 빈 표
            if ti['kind'] in ('compare', 'list', 'multi', 'factor'):
                filled = [tc for tr in data for tc in tr if H.local(tc) == 'tc' and H.cell_text(tc).strip()]
                if not data or not filled:
                    rep.add('확인', '빈 표', f'{ti["id"]} ({b["name"]})', '데이터 행이 없거나 모두 비어 있음. "해당없음" 처리 대상인지 확인')
            if ti['kind'] == 'compare':
                cur_c, prev_c, d_c, r_c, pg_c = ro.get('cur'), ro.get('prev'), ro.get('diff'), ro.get('rate'), ro.get('page')
                total_rows = []
                blank_cells = []
                want = direction_of(heading_before(tbl)) if in_scope(tbl) else 0
                for tr in data:
                    cells = {H.cell_info(tc)[1]: tc for tc in tr if H.local(tc) == 'tc'}
                    if cur_c not in cells or prev_c not in cells:
                        continue
                    lab = ' '.join(H.cell_text(tc) for c, tc in cells.items() if c not in (cur_c, prev_c, d_c, r_c, pg_c))
                    (cl, cf) = cell_lines_numbers(H.cell_text(cells[cur_c]))
                    (pl, pf) = cell_lines_numbers(H.cell_text(cells[prev_c]))
                    (dl, df) = cell_lines_numbers(H.cell_text(cells[d_c])) if d_c in cells else ([], {})
                    (rl, rf) = cell_lines_numbers(H.cell_text(cells[r_c])) if r_c in cells else ([], {})
                    where = f'{ti["id"]}:r{H.cell_info(cells[cur_c])[0]} ({b["name"]})'
                    for c in (cur_c, prev_c, d_c):
                        if c in cells and first_line_blank(H.cell_text(cells[c])):
                            blank_cells.append(f'r{H.cell_info(cells[c])[0]}c{c}')
                    if cl and cl[0] and pl and pl[0]:
                        try:
                            cv, pv = to_int(cl[0]), to_int(pl[0])
                            pairs.append((cv, pv, K.UNIT_WON.get(ti.get('unit') or '천원', 1000)))
                            if re.sub(r'\s', '', lab) in ('합계', '계', '총계'):
                                total_rows.append((tr, cv))
                        except Exception:  # noqa: BLE001
                            pass
                    if want and dl and dl[0] and re.sub(r'\s', '', lab) not in ('합계', '계', '총계', '소계'):
                        try:
                            dv = to_int(dl[0])
                            if dv * want <= 0:
                                rep.add('확인', '증감 방향', where, f'표 제목은 {"증액" if want > 0 else "감액"}인데 증감액이 {dl[0]} | {H.short(lab, 40)}')
                        except Exception:  # noqa: BLE001
                            pass
                    # 증감 칸이 '-' 인데 올해와 전년이 다름 (작년 문서에서 자주 보이는 잘못)
                    if d_c in cells and cl and pl and cl[0] and pl[0]:
                        dtxt = H.cell_text(cells[d_c]).strip().split('\n')[0].strip()
                        try:
                            if dtxt in ('-', '－', '') and to_int(cl[0]) != to_int(pl[0]):
                                rep.add('오류', '증감 표기', where, f'올해 {cl[0]}, 전년 {pl[0]} 인데 증감 칸이 "{dtxt or "빈칸"}"')
                        except Exception:  # noqa: BLE001
                            pass
                    checks = []
                    if cl and pl and dl and cl[0] and pl[0] and dl[0]:
                        checks.append(('총액', cl[0], pl[0], dl[0]))
                    for fund in set(cf) & set(pf) & set(df):
                        checks.append((fund, cf[fund], pf[fund], df[fund]))
                    for name, c_, p_, d_ in checks:
                        try:
                            if to_int(c_) - to_int(p_) != to_int(d_):
                                rep.add('오류', '증감액 계산', where, f'[{name}] {c_} - {p_} = {K.fmt_num(to_int(c_) - to_int(p_))} 인데 {d_}')
                        except Exception:  # noqa: BLE001
                            pass
                    if rl and rl[0] and cl and cl[0] and pl and pl[0]:
                        try:
                            pv = to_int(pl[0])
                            if pv:
                                digits = len(rl[0].split('.')[1]) if '.' in rl[0] else 0
                                exp = K.rate(to_int(cl[0]) - pv, pv, digits, rate_mode)
                                if nrm(exp) != nrm(rl[0]) or (exp.startswith('△') != rl[0].startswith('△')):
                                    alt = K.rate(to_int(cl[0]) - pv, pv, digits, 'trunc' if rate_mode == 'round' else 'round')
                                    note = ' (버림/반올림 차이)' if nrm(alt) == nrm(rl[0]) else ''
                                    rep.add('오류', '증감률 계산', where, f'{rl[0]} 인데 계산값 {exp}{note}')
                        except Exception:  # noqa: BLE001
                            pass
                    # 쪽번호
                    if pg_c in cells and printed and cl and cl[0]:
                        pg = [x.replace(',', '') for x in re.findall(r'\d{1,3}(?:,\d{3})+|\d{1,4}', H.cell_text(cells[pg_c]))]
                        val = nrm(cl[0])
                        if pg and val not in ('0',):
                            ok = any(val in printed.get(int(x), set()) for x in pg) or \
                                any(val in printed.get(int(x) + d, set()) for x in pg for d in (-1, 1))
                            if not ok and int(pg[0]) in printed:
                                rep.add('확인', '쪽번호 대조', where, f'{H.cell_text(cells[pg_c]).strip()}쪽(앞뒤 1쪽 포함)에 {cl[0]}이(가) 없음')
                if blank_cells:
                    rep.add('확인', '칸 첫 줄 비어 있음', f'{ti["id"]} ({b["name"]})',
                            f'{len(blank_cells)}칸: 숫자 위에 빈 줄이 있어 한 줄 아래로 밀려 보임 ({", ".join(blank_cells[:6])}{" …" if len(blank_cells) > 6 else ""})')
                # 합계 행 = 나머지 행의 합
                if total_rows and len(data) > 2:
                    tr0, tv = total_rows[0]
                    vals, subs = [], []
                    for tr in data:
                        if tr is tr0:
                            continue
                        cells = {H.cell_info(tc)[1]: tc for tc in tr if H.local(tc) == 'tc'}
                        if cur_c in cells:
                            n, _ = cell_lines_numbers(H.cell_text(cells[cur_c]))
                            lab = re.sub(r'\s', '', ' '.join(H.cell_text(tc) for c, tc in cells.items() if c < cur_c))
                            if n and n[0]:
                                try:
                                    (subs if '소계' in lab else vals).append(to_int(n[0]))
                                except Exception:  # noqa: BLE001
                                    pass
                    if vals and tv not in (sum(vals), sum(subs) if subs else None):
                        rep.add('확인', '합계 행', f'{ti["id"]} ({b["name"]})', f'합계 {K.fmt_num(tv)} / 아래 행 합 {K.fmt_num(sum(vals))}'
                                + (f' / 소계 합 {K.fmt_num(sum(subs))}' if subs else '') + ' (표가 일부 항목만 보여주는 경우면 정상)')
            # 원자료에 없는 숫자
            if src_tok is not None and ti['kind'] in ('compare', 'list', 'multi'):
                skip_cols = {ro.get('diff'), ro.get('rate'), ro.get('page')}
                won_table = (ti.get('unit') or '천원') == '원'
                for tr in data:
                    pages = set()
                    for tc in tr:
                        if H.local(tc) == 'tc' and H.cell_info(tc)[1] == ro.get('page'):
                            for x in re.findall(r'\d{1,3}(?:,\d{3})+|\d{1,4}', H.cell_text(tc)):
                                pg = int(x.replace(',', ''))
                                pages |= {pg - 1, pg, pg + 1}
                    pages = {p for p in pages if p in PLAIN}
                    for tc in tr:
                        if H.local(tc) != 'tc':
                            continue
                        r, c, rs, cs = H.cell_info(tc)
                        if c in skip_cols or c == ro.get('desc'):
                            continue
                        seen_cell = set()
                        for t in tokens(H.cell_text(tc)):
                            key = nrm(t)
                            if '.' in key or len(key) < 4 or key in seen_cell:
                                continue
                            seen_cell.add(key)
                            pool_ok = key in (src_tok if won_table else SRC_PLAIN) or key in aux_tok
                            if not pool_ok:
                                tag = ' (작년 문서에도 있던 값)' if key in old_tok else ''
                                rep.add('확인', '원자료에 없는 숫자', f'{ti["id"]}:r{r}:c{c} ({b["name"]})', f'{t}{tag}')
                            elif pages and not won_table and key not in aux_tok and not any(key in PLAIN[p] for p in pages):
                                rep.add('확인', '적힌 쪽에 없는 숫자', f'{ti["id"]}:r{r}:c{c} ({b["name"]})',
                                        f'{t} 이(가) 이 행의 예산서 쪽(앞뒤 1쪽 포함)에 없음. 다른 쪽 값이거나 계산한 값이면 확인')
        # 3) 금액 문장
        for s in b['sentences']:
            txt = s['text']
            if '변동' in txt and not K.find_amounts(txt):
                continue
            amts = K.find_amounts(txt)
            m = K.RATE_RE.search(txt)
            if not amts:
                continue
            first_won = amts[0][2]
            matched = None
            for cv, pv, unit_won in pairs:
                if K.fmt_won(cv * unit_won, txt[amts[0][0]:amts[0][1]]) == txt[amts[0][0]:amts[0][1]].strip():
                    matched = (cv, pv, unit_won)
                    break
            if matched is None:
                for cv, pv, unit_won in pairs:
                    if abs(cv * unit_won - first_won) <= 1_000_000:
                        matched = (cv, pv, unit_won)
                        break
            if matched is None:
                if any(abs(v - first_won) <= 500_000 for v in near_values(first_won)):
                    rep.add('확인', '금액 문장', f'{s["para"]} ({b["name"]})', f'첫 금액이 이 블록 비교표에는 없고 다른 블록 표나 원자료에 있음. 그 값을 쓴 문장이면 정상 | {H.short(txt, 90)}')
                else:
                    rep.add('오류', '금액 문장', f'{s["para"]} ({b["name"]})', f'첫 금액이 블록의 어느 비교표 값과도 맞지 않음 | {H.short(txt, 90)}')
                continue
            unit_name = {1: '원', 1000: '천원', 1_000_000: '백만원', 10_000: '만원'}.get(matched[2], '천원')
            new_s, why = K.rewrite_sentence(txt, matched[0], matched[1], unit_name, None, rate_mode)
            if new_s and re.sub(r'\s+', '', new_s) != re.sub(r'\s+', '', txt):
                rep.add('오류', '금액 문장', f'{s["para"]} ({b["name"]})', f'표 값({K.fmt_num(matched[0])}, 전년 {K.fmt_num(matched[1])})과 다름\n      지금: {txt}\n      제안: {new_s}')
    # 3-1) 기존, 변경, 증감 묶음 행 (계속비 표 등): 증감 = 변경 - 기존
    for tbl in doc.tables:
        if not in_scope(tbl):
            continue
        cells = [(H.cell_info(x), H.cell_text(x).strip()) for tr in H.tbl_rows(tbl) for x in tr if H.local(x) == 'tc']
        by_row = {}
        for (r, c, rs, cs), t in cells:
            by_row.setdefault(r, {})[c] = t
        sub = {}
        for r, row in by_row.items():
            for c in sorted(row):
                w = re.sub(r'\(.*?\)|\s', '', row[c])
                if w in ('기존', '당초', '변경', '증감', '계획', '조정'):
                    sub[r] = (c, w)
                    break
        first_sub = min(sub) if sub else 0
        skip_c = set()
        for (r, c, rs, cs), t in cells:
            if r < first_sub and re.search(r'페이지|쪽|비고|사업명|개요', re.sub(r'\s', '', t)):
                skip_c |= set(range(c, c + cs))
        for r, (c0, w) in sub.items():
            if w != '증감' or r - 2 not in sub or r - 1 not in sub:
                continue
            a_row, b_row, d_row = by_row[r - 2], by_row[r - 1], by_row[r]
            if all((b_row.get(c) or '').strip() in ('-', '－', '') for c in d_row if c > c0 and c not in skip_c):
                continue            # 변경 행이 모두 '-' 이면 '변경 없음' 표기로 봄
            for c in sorted(d_row):
                if c <= c0 or c in skip_c:
                    continue

                def val(t):
                    t = (t or '').strip().split('\n')[0].strip()
                    if t in ('-', '－', ''):
                        return 0
                    m = re.fullmatch(r'△?\d[\d,]*', t)
                    return to_int(t) if m else None
                a_, b_, d_ = val(a_row.get(c)), val(b_row.get(c)), val(d_row.get(c))
                if None in (a_, b_, d_):
                    continue
                if b_ - a_ != d_:
                    rep.add('오류', '증감 행 계산', f'{doc.tid_of(tbl)}:r{r}:c{c}',
                            f'변경 {K.fmt_num(b_)} - 기존 {K.fmt_num(a_)} = {K.fmt_num(b_ - a_) if b_ != a_ else 0} 인데 증감 칸이 "{d_row.get(c) or "빈칸"}"')
    # 3-2) 문서 속성 제목
    if only_block is None and year:
        hpf = doc.raw.get('Contents/content.hpf', b'').decode('utf-8', 'replace')
        m = re.search(r'<opf:title>(.*?)</opf:title>', hpf, re.S)
        if m and re.search(rf'(?<!\d){year - 1}(?!\d)', m.group(1)) and not re.search(rf'(?<!\d){year}(?!\d)', m.group(1)):
            rep.add('확인', '문서 속성 제목', os.path.basename(new), f'"{m.group(1).strip()}" (작년 연도. apply --title 로 바꾸기)')
    # 4) 연도, 미완성, 자리표시, 검토 필요
    if year:
        for si, i, p in doc.top_paras():
            if not in_scope(p):
                continue
            for q in p.iter(H.HP + 'p'):
                if q.find('.//' + H.HP + 'p') is not None:
                    continue
                txt = H.para_text(q)
                if not txt.strip():
                    continue
                where = f's{si}:p{i}'
                tb = next(q.iterancestors(H.HP + 'tbl'), None)
                if tb is not None:
                    where = doc.tid_of(tb) or where
                if re.search(rf'(?<![\d,]){year - 2}(?![\d,])', txt) or re.search(rf"[`‘’']{(year - 2) % 100:02d}\.", txt):
                    rep.add('확인', '지난 연도 표기', where, f'{year - 2}년이 남아 있음 (과거 이력이면 정상) | {H.short(txt, 80)}')
                if re.search(r'(증가|감소)한\s*$|보다\s*[\d.]+%\s*$', txt.strip()):
                    rep.add('확인', '미완성 문장', where, H.short(txt, 90))
                if re.search(r'○○|XX|ＸＸ|\?\?|\[작성\]|TODO', txt):
                    rep.add('확인', '자리표시', where, H.short(txt, 90))
                if '검토 필요' in txt:
                    rep.add('정보', '[검토 필요] 표시', where, H.short(txt, 90))
    if old_doc is not None:
        check_conventions(doc, old_doc, res, A.analyze(old, old_year), rep, in_scope)
    if old_doc is not None and detected and detected == old_year and only_block is None:
        rep.add('오류', '연도', os.path.basename(new), f'문서 기준 연도가 작년 문서와 같음({detected}). 제목과 표 머리 연도를 바꿨는지 확인')
    return rep


def main():
    try:
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--new', required=True)
    ap.add_argument('--old')
    ap.add_argument('--index')
    ap.add_argument('--rate-mode', default='round', choices=['round', 'trunc'])
    ap.add_argument('--block', type=int)
    ap.add_argument('--json')
    ap.add_argument('--limit', type=int, default=40)
    ap.add_argument('--year', type=int, help='새 문서의 기준 연도 (기본: 작년 문서 연도 + 1)')
    a = ap.parse_args()
    rep = verify(a.new, a.old, a.index, a.rate_mode, a.block, year=a.year)
    rep.print(a.limit)
    if a.json:
        json.dump(rep.items, open(a.json, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    sys.exit(1 if any(i['level'] == '오류' for i in rep.items) else 0)


if __name__ == '__main__':
    main()
