#!/usr/bin/env python3
"""HWPX 문서를 읽고, 주소로 가리키고, 서식을 지키며 고쳐 쓰는 라이브러리 겸 CLI.

주소 체계
  s{섹션}:p{번호}      섹션 루트의 단락 (0부터)
  t{번호}              문서 전체에서 표의 순서 (중첩 표 포함, 0부터)
  t{번호}:r{행}:c{열}  표의 칸 (cellAddr 기준, 병합 칸은 시작 위치)

CLI
  python3 hwpx_ops.py info    doc.hwpx
  python3 hwpx_ops.py outline doc.hwpx                  # 제목 상자, 머리말 단락, 표 목록
  python3 hwpx_ops.py dump    doc.hwpx [--from s0:p10] [--to s0:p60] [--full]
  python3 hwpx_ops.py table   doc.hwpx t12 [--full]     # 표 하나를 격자로
  python3 hwpx_ops.py find    doc.hwpx "검색어"          # 글자가 있는 주소
  python3 hwpx_ops.py text    doc.hwpx > doc.txt         # 전체 글
  python3 hwpx_ops.py apply   doc.hwpx plan.json -o out.hwpx [--title "새 제목"]

편집 계획(plan.json) 형식은 references/edit_plan.md 참고.
"""
import argparse
import signal
import copy
import os
import difflib
import json
import random
import re
import sys

sys.dont_write_bytecode = True     # skill 폴더에 __pycache__ 를 만들지 않음
import zipfile

try:
    from lxml import etree
except ImportError:  # pragma: no cover
    sys.exit('lxml이 필요합니다: pip install lxml --break-system-packages')

HP = '{http://www.hancom.co.kr/hwpml/2011/paragraph}'
OPF = '{http://www.idpf.org/2007/opf/}'

# ---------------------------------------------------------------- 기본 탐색


def local(el):
    return el.tag.split('}')[-1] if isinstance(el.tag, str) else ''


def own_runs(p):
    return [r for r in p if local(r) == 'run']


def own_t_nodes(p):
    out = []
    for r in own_runs(p):
        for c in r:
            if local(c) == 't':
                out.append(c)
    return out


MARKERS = {'tab': '\t', 'lineBreak': '\n', 'fwSpace': ' ', 'nbSpace': ' ', 'hyphen': '-'}


def segments(p):
    """단락의 글자를 (객체, 속성, 글자) 조각 목록으로. 탭 같은 특수 요소는 고정 표식."""
    segs = []
    for t in own_t_nodes(p):
        segs.append([t, 'text', t.text or '', True])
        for ch in t:
            m = MARKERS.get(local(ch))
            if m is not None:
                segs.append([ch, None, m, False])
            segs.append([ch, 'tail', ch.tail or '', True])
    return segs


def para_text(p):
    return ''.join(s[2] for s in segments(p))


def is_cell_para(p):
    par = p.getparent()
    return par is not None and local(par) == 'subList'


def drop_lineseg(p):
    for ls in p.findall(HP + 'linesegarray'):
        p.remove(ls)


TOKEN_RE = re.compile(r'△?\d[\d,]*(?:\.\d+)?[조억천백십만원%]*|[가-힣A-Za-z]+|\s+|.', re.S)


def token_opcodes(old, new):
    """숫자, 단어, 공백 단위로 비교한 뒤 글자 위치로 바꾼 편집 목록."""
    ta = [(m.start(), m.end()) for m in TOKEN_RE.finditer(old)]
    tb = [(m.start(), m.end()) for m in TOKEN_RE.finditer(new)]
    wa = [old[a:b] for a, b in ta]
    wb = [new[a:b] for a, b in tb]
    sm = difflib.SequenceMatcher(None, wa, wb, autojunk=False)
    out = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == 'equal':
            continue
        c1 = ta[i1][0] if i1 < len(ta) else len(old)
        c2 = ta[i2 - 1][1] if i2 > i1 else c1
        d1 = tb[j1][0] if j1 < len(tb) else len(new)
        d2 = tb[j2 - 1][1] if j2 > j1 else d1
        out.append((tag, c1, c2, d1, d2))
    return out


def set_text(p, new):
    """단락 글자를 new로 바꾼다. 바뀐 부분만 고쳐서 글자 모양(run)을 최대한 유지."""
    segs = segments(p)
    old = ''.join(s[2] for s in segs)
    if old == new:
        return False
    if not any(s[3] for s in segs):
        runs = own_runs(p)
        if runs:
            run = runs[0]
        else:
            run = etree.Element(HP + 'run')
            run.set('charPrIDRef', '0')
            ls = p.find(HP + 'linesegarray')
            if ls is not None:
                ls.addprevious(run)
            else:
                p.append(run)
        t = etree.SubElement(run, HP + 't')
        t.text = new
        drop_lineseg(p)
        return True
    starts, lens = [], []
    pos = 0
    for sg in segs:
        starts.append(pos)
        lens.append(len(sg[2]))
        pos += len(sg[2])
    total = pos

    def owner(i):
        for k in range(len(segs)):
            if starts[k] <= i < starts[k] + lens[k]:
                return k
        return None

    def nearest_editable(k, forward=True):
        order = list(range(k, len(segs))) + list(range(k - 1, -1, -1)) if forward else \
            list(range(k, -1, -1)) + list(range(k + 1, len(segs)))
        for j in order:
            if segs[j][3]:
                return j
        return None

    def insert_point(i, is_insert):
        # 바꾸기: 지워지는 첫 글자의 조각에 / 끼워넣기: 바로 앞 글자의 조각 끝에
        if is_insert and i > 0:
            k = owner(i - 1)
            if k is not None and segs[k][3]:
                return k, i - starts[k]
            k = nearest_editable(k if k is not None else 0, forward=False)
            return k, (i - starts[k]) if starts[k] <= i <= starts[k] + lens[k] else len(segs[k][2])
        k = owner(i) if i < total else None
        if k is None:
            k = nearest_editable(len(segs) - 1, forward=False)
            return k, len(segs[k][2])
        if segs[k][3]:
            return k, i - starts[k]
        k2 = nearest_editable(k, forward=False)
        if k2 is not None and k2 < k:
            return k2, len(segs[k2][2])
        k2 = nearest_editable(k)
        return k2, 0

    ops = token_opcodes(old, new)
    removed = set()
    for tag, i1, i2, j1, j2 in reversed(ops):
        if i2 > i1:
            for k in range(len(segs)):
                a, b = starts[k], starts[k] + lens[k]
                lo, hi = max(a, i1), min(b, i2)
                if lo < hi:
                    if segs[k][3]:
                        txt = segs[k][2]
                        segs[k][2] = txt[:lo - a] + txt[hi - a:]
                    else:
                        removed.add(k)
        if j2 > j1:
            k, off = insert_point(i1, tag == 'insert')
            txt = segs[k][2]
            segs[k][2] = txt[:off] + new[j1:j2] + txt[off:]
    for k, sg in enumerate(segs):
        obj, attr, txt = sg[0], sg[1], sg[2]
        if attr == 'text':
            obj.text = txt
        elif attr == 'tail':
            obj.tail = txt
    for k in sorted(removed, reverse=True):
        obj = segs[k][0]
        parent = obj.getparent()
        if parent is None:
            continue
        tail = obj.tail or ''
        prev = obj.getprevious()
        if prev is not None:
            prev.tail = (prev.tail or '') + tail
        else:
            parent.text = (parent.text or '') + tail
        parent.remove(obj)
    drop_lineseg(p)
    return True


# ---------------------------------------------------------------- 표


def tbl_rows(tbl):
    return [tr for tr in tbl if local(tr) == 'tr']


def cell_info(tc):
    a = tc.find(HP + 'cellAddr')
    s = tc.find(HP + 'cellSpan')
    return (int(a.get('rowAddr')), int(a.get('colAddr')),
            int(s.get('rowSpan')) if s is not None else 1, int(s.get('colSpan')) if s is not None else 1)


def set_addr(tc, r=None, c=None):
    a = tc.find(HP + 'cellAddr')
    if r is not None:
        a.set('rowAddr', str(r))
    if c is not None:
        a.set('colAddr', str(c))


def set_span(tc, rs=None, cs=None):
    s = tc.find(HP + 'cellSpan')
    if rs is not None:
        s.set('rowSpan', str(rs))
    if cs is not None:
        s.set('colSpan', str(cs))


def cell_height(tc):
    sz = tc.find(HP + 'cellSz')
    return int(sz.get('height')) if sz is not None else 0


def set_cell_height(tc, h):
    sz = tc.find(HP + 'cellSz')
    if sz is not None:
        sz.set('height', str(max(1, int(h))))


def cell_paras(tc):
    sub = tc.find(HP + 'subList')
    return [p for p in sub if local(p) == 'p'] if sub is not None else []


def cell_text(tc):
    return '\n'.join(para_text(p) for p in cell_paras(tc))


def cells_of(tbl):
    out = {}
    for tr in tbl_rows(tbl):
        for tc in tr:
            if local(tc) == 'tc':
                r, c, rs, cs = cell_info(tc)
                out[(r, c)] = tc
    return out


def strip_fields(el):
    """복제한 내용의 수식 필드를 없애고 글자만 남김 (중복 id 방지)."""
    for ctrl in list(el.iter(HP + 'ctrl')):
        if any(local(c) in ('fieldBegin', 'fieldEnd') for c in ctrl):
            ctrl.getparent().remove(ctrl)


def update_formula(p):
    """칸 안 수식 필드의 마지막 결과값을 현재 글자로 맞춤."""
    for fb in p.iter(HP + 'fieldBegin'):
        if fb.get('type') != 'FORMULA':
            continue
        val = para_text(p).strip()
        for sp in fb.iter(HP + 'stringParam'):
            if sp.get('name') == 'LastResult':
                sp.text = val
            elif sp.get('name') == 'Command' and sp.text and ';;' in sp.text:
                sp.text = sp.text.rsplit(';;', 1)[0] + ';;' + val


def line_marker(s):
    """줄의 역할을 앞 기호로 나눔: 제목(∘ ○), 항목(- ·), 강조(▷ ▸), 주석(※), 번호(1.), 빈 줄, 그 밖."""
    t = (s or '').lstrip(' \t\u3000')
    if not t:
        return ''
    ch = t[0]
    if ch in '∘○◦•●◎□■':
        return 'o'
    if ch in '-－‐–ㆍ·':
        return '-'
    if ch in '▷▶▸►':
        return '>'
    if ch in '※*':
        return '*'
    if re.match(r'\d+\s*[.)]', t) or re.match(r'[가-하]\s*[.)]', t):
        return '1'
    if ch in '(（':
        return '('
    return 'a'


def _has_ctrl(p):
    return any(local(x) in ('tbl', 'pic', 'container', 'equation', 'ole') for x in p.iter())


def _run_text(ru):
    out = []
    for c in ru:
        if local(c) == 't':
            out.append(c.text or '')
            for ch in c:
                out.append(MARKERS.get(local(ch), '') + (ch.tail or ''))
    return ''.join(out)


def _dominant_char(paras):
    """단락들에서 글자 수가 가장 많은 글자 모양."""
    from collections import Counter
    c = Counter()
    for p in paras:
        for ru in own_runs(p):
            n = len(_run_text(ru).strip())
            if n:
                c[ru.get('charPrIDRef')] += n
    return c.most_common(1)[0][0] if c else None


def _similar(a, b):
    from difflib import SequenceMatcher
    wa = TOKEN_RE.findall(a)
    wb = TOKEN_RE.findall(b)
    return SequenceMatcher(None, wa, wb, autojunk=False).ratio() if (wa or wb) else 1.0


def _normalize_runs(p, old_text, new_text, char_id):
    """내용을 거의 새로 쓴 줄: 앞쪽 공통 부분(예: '  - 내    용 : ') 뒤의 글자 모양을 한 가지로 맞춤.
    작년 줄의 일부 강조나 작은 글씨가 엉뚱한 글자에 붙는 것을 막는다."""
    if not char_id or _similar(old_text, new_text) >= 0.6:
        return
    styles = {ru.get('charPrIDRef') for ru in own_runs(p) if _run_text(ru).strip()}
    if len(styles) < 2:
        return                      # 글자 모양이 한 가지인 줄은 그대로
    if char_id not in styles:
        char_id = _dominant_char([p])
    k = len(os.path.commonprefix([old_text, new_text]))
    pos = 0
    for ru in own_runs(p):
        t = _run_text(ru)
        end = pos + len(t)
        if t.strip() and end > k:
            ru.set('charPrIDRef', char_id)
        pos = end


def _cell_line_key(text):
    """제목 줄('∘ 편성사유')이면 기호 뒤 첫 낱말, 아니면 None."""
    t = (text or '').strip().lstrip('∘○◦•●◎□■0123456789.)가나다라마바사 ')
    m = re.match(r'[가-힣A-Za-z]+', t.replace(' ', ''))
    return m.group(0)[:4] if m else None


def _sections(texts, marks):
    """줄마다 속한 제목(∘, 1. 줄의 첫 낱말)."""
    sec, out = None, []
    for t, m in zip(texts, marks):
        if m in ('o', '1'):
            sec = _cell_line_key(t)
        out.append(sec)
    return out


def set_cell_text(tc, text):
    """칸 글자 바꾸기. 줄마다 같은 역할(앞 기호)의 원래 줄 서식을 쓴다.

    원래 줄과 새 줄의 역할이 위치마다 같으면 제자리에서 글자만 바꾼다(수식, 글자 모양 유지).
    줄 수나 순서가 다르면 '∘ 편성사유'는 원래 '∘ 편성사유' 줄의, 그 아래 '  - ...'는 원래 그 제목 아래 '-' 줄의
    서식을 복제해 쓴다. 내용을 거의 새로 쓴 줄은 공통 앞부분 뒤를 그 역할의 주된 글자 모양으로 맞춘다.
    """
    lines = text.split('\n') if text is not None else ['']
    paras = cell_paras(tc)
    sub = tc.find(HP + 'subList')
    if not paras:
        return False
    old_texts = [para_text(p) for p in paras]
    old_m = [line_marker(t) for t in old_texts]
    new_m = [line_marker(l) for l in lines]
    old_sec = _sections(old_texts, old_m)
    new_sec = _sections(lines, new_m)
    dom = {}
    for m in set(old_m):
        dom[m] = _dominant_char([p for p, om in zip(paras, old_m) if om == m])
    for key in set(zip(old_m, old_sec)):
        dom[key] = _dominant_char([p for p, om, os_ in zip(paras, old_m, old_sec) if (om, os_) == key])
    dom_all = _dominant_char(paras)

    def style_for(i):
        return dom.get((new_m[i], new_sec[i])) or dom.get(new_m[i]) or dom_all
    positional = (len(lines) == len(paras) and all(a == b or not a or not b for a, b in zip(old_m, new_m))) \
        or any(_has_ctrl(p) for p in paras) or len(set(m for m in old_m if m)) <= 1
    if positional:
        while len(paras) < len(lines):
            np_ = copy.deepcopy(paras[-1])
            strip_fields(np_)
            drop_lineseg(np_)
            paras[-1].addnext(np_)
            paras.append(np_)
            old_texts.append(old_texts[-1])
        changed = False
        while len(paras) > len(lines) and len(paras) > 1:
            sub.remove(paras[-1])
            paras.pop()
            changed = True
        for i, (p, line, ot) in enumerate(zip(paras, lines, old_texts)):
            if set_text(p, line):
                changed = True
                update_formula(p)
                _normalize_runs(p, ot, line, style_for(i))
        return changed
    # 역할 맞추기: 제목 줄은 같은 제목끼리, 그 밖의 줄은 같은 제목 아래 같은 기호끼리
    texted = [j for j, t in enumerate(old_texts) if t.strip()] or list(range(len(paras)))
    chosen = []
    for i, m in enumerate(new_m):
        if not m:
            cands = [j for j, om in enumerate(old_m) if not om] or [min(i, len(paras) - 1)]
        else:
            same = [j for j, om in enumerate(old_m) if om == m]
            exact = [j for j in same if old_sec[j] == new_sec[i]]
            cands = exact or same or texted
        chosen.append(min(cands, key=lambda j: (abs(j - i), j)))
    used = set()
    new_ps = []
    for j in chosen:
        if j not in used:
            used.add(j)
            new_ps.append(paras[j])
        else:
            np_ = copy.deepcopy(paras[j])
            strip_fields(np_)
            drop_lineseg(np_)
            new_ps.append(np_)
    first_index = list(sub).index(paras[0])
    for p in paras:
        sub.remove(p)
    for k, p in enumerate(new_ps):
        sub.insert(first_index + k, p)
    for i, (p, line, j) in enumerate(zip(new_ps, lines, chosen)):
        set_text(p, line)
        update_formula(p)
        _normalize_runs(p, old_texts[j], line, style_for(i))
    return True


def clear_cell(tc):
    set_cell_text(tc, '')


def new_id():
    return str(random.randint(10 ** 9, 2 ** 31 - 1))


# ---------------------------------------------------------------- 굵게, 들여쓰기 (작년 서식 따르기)

HH = '{http://www.hancom.co.kr/hwpml/2011/head}'
BOLD_MARK = re.compile(r'\*\*(.+?)\*\*', re.S)
LEAD = re.compile(r'^(\s*)([-○◦∘•▸▶▷※·ㆍ]|\d+\.|[가-하]\.|\(\d+\))\s')


def keep_indent(model_text, new_text):
    """새 글이 같은 기호로 시작하는데 앞 빈칸이 없으면 견본 단락의 앞 빈칸을 붙인다.
    ('- 22억...' -> '    - 22억...' : 작년 줄이 빈칸 4개 뒤 '-' 였으면)"""
    m0 = LEAD.match(model_text or '')
    m1 = LEAD.match(new_text.lstrip('*') if new_text else '')
    if m0 and m1 and m0.group(2) == m1.group(2) and not new_text[:1].isspace() and m0.group(1):
        return m0.group(1) + new_text
    return new_text


def bold_ids(doc):
    hdr = doc.header()
    return {c.get('id') for c in hdr.iter(HH + 'charPr') if c.find(HH + 'bold') is not None}


def bold_variant(doc, char_id):
    """char_id 글자 모양의 굵은 판. 없으면 header 에 새로 만든다."""
    hdr = doc.header()
    cps = {c.get('id'): c for c in hdr.iter(HH + 'charPr')}
    base = cps.get(char_id)
    if base is None:
        return char_id
    if base.find(HH + 'bold') is not None:
        return char_id
    key = etree.tostring(base).replace(f'id="{char_id}"'.encode(), b'')
    for cid, c in cps.items():
        if c.find(HH + 'bold') is not None:
            c2 = copy.deepcopy(c)
            c2.remove(c2.find(HH + 'bold'))
            if etree.tostring(c2).replace(f'id="{cid}"'.encode(), b'') == key:
                return cid
    nc = copy.deepcopy(base)
    nid = str(max(int(k) for k in cps) + 1)
    nc.set('id', nid)
    b = etree.Element(HH + 'bold')
    anchor = next((x for x in (nc.find(HH + n) for n in ('underline', 'strikeout', 'outline', 'shadow')) if x is not None), None)
    if anchor is not None:
        anchor.addprevious(b)          # 순서: ... offset, italic, bold, underline, strikeout, outline, shadow
    else:
        nc.append(b)
    parent = base.getparent()
    parent.append(nc)
    parent.set('itemCnt', str(len(parent)))
    doc.header_dirty = True
    return nid


REVIEW_MARK = '[검토 필요]'
REVIEW_COLOR = '#FF0000'


def color_variant(doc, char_id, color):
    """char_id 글자 모양에서 글자색만 color 로 바꾼 판. 같은 것이 있으면 그것, 없으면 header 에 새로 만든다."""
    hdr = doc.header()
    cps = {c.get('id'): c for c in hdr.iter(HH + 'charPr')}
    base = cps.get(char_id)
    if base is None or base.get('textColor', '').upper() == color:
        return char_id

    def key(c):
        c2 = copy.deepcopy(c)
        c2.attrib.pop('id', None)
        c2.set('textColor', color)
        return etree.tostring(c2)
    want = key(base)
    for cid, c in cps.items():
        if c.get('textColor', '').upper() == color and key(c) == want:
            return cid
    nc = copy.deepcopy(base)
    nid = str(max(int(k) for k in cps) + 1)
    nc.set('id', nid)
    nc.set('textColor', color)
    parent = base.getparent()
    parent.append(nc)
    parent.set('itemCnt', str(len(parent)))
    doc.header_dirty = True
    return nid


def color_review_marks(doc):
    """문서 전체(표 칸 포함)의 '[검토 필요]' 글자를 빨간 글자로 바꾼다. 사람이 찾아 지우기 쉽게.
    표시가 든 글 조각(run)을 앞 글, 표시, 뒤 글로 나눈다. 이미 빨간 것은 건드리지 않음."""
    n = 0
    for root in doc.sections:
        changed = True
        while changed:
            changed = False
            for run in list(root.iter(HP + 'run')):
                ts = [c for c in run if local(c) == 't']
                if len(ts) != 1 or len(run) != 1 or len(ts[0]) or REVIEW_MARK not in (ts[0].text or ''):
                    continue
                cid = run.get('charPrIDRef')
                red = color_variant(doc, cid, REVIEW_COLOR)
                if red == cid:
                    continue
                text = ts[0].text
                i = text.index(REVIEW_MARK)
                pieces = [(cid, text[:i]), (red, REVIEW_MARK), (cid, text[i + len(REVIEW_MARK):])]
                for c_id, t in pieces:
                    if not t:
                        continue
                    nr = etree.Element(HP + 'run', dict(run.attrib))
                    nr.set('charPrIDRef', c_id)
                    etree.SubElement(nr, HP + 't').text = t
                    run.addprevious(nr)
                run.getparent().remove(run)
                n += 1
                changed = True
                break
    return n


def _char_props(doc, cid):
    c = doc.__dict__.setdefault('_cp_cache', {})
    if cid not in c:
        cp = next((x for x in doc.header().iter(HH + 'charPr') if x.get('id') == cid), None)
        h = int(cp.get('height', '1000')) if cp is not None else 1000
        ra = cp.find(HH + 'ratio') if cp is not None else None
        sp = cp.find(HH + 'spacing') if cp is not None else None
        c[cid] = (h, int(ra.get('hangul', '100')) if ra is not None else 100, int(sp.get('hangul', '0')) if sp is not None else 0)
    return c[cid]


def text_width(text, height, ratio, spacing):
    """한 줄 폭 어림 (HWPUNIT). 한글 등 전각 글자는 글자 크기만큼, 숫자, 영문, 문장부호는 절반, 빈칸은 0.45.
    자간(spacing, %)은 글자마다 글자 크기의 그 비율만큼 더하거나 뺀다."""
    w = 0.0
    for ch in text:
        k = 0.45 if ch == ' ' else (1.0 if ord(ch) > 0x2000 and ch not in '‘’“”' else 0.5)
        w += height * ratio / 100 * k + height * spacing / 100 * k
    return w


def spacing_variant(doc, char_id, value):
    """char_id 글자 모양에서 자간만 value(%)로 바꾼 판."""
    hdr = doc.header()
    cps = {c.get('id'): c for c in hdr.iter(HH + 'charPr')}
    base = cps.get(char_id)
    if base is None or base.find(HH + 'spacing') is None:
        return char_id

    def key(c, v):
        c2 = copy.deepcopy(c)
        c2.attrib.pop('id', None)
        sp = c2.find(HH + 'spacing')
        for k in list(sp.attrib):
            sp.set(k, str(v))
        return etree.tostring(c2)
    want = key(base, value)
    for cid, c in cps.items():
        if c.find(HH + 'spacing') is not None and key(c, value) == want and c.find(HH + 'spacing').get('hangul') == str(value):
            return cid
    nc = copy.deepcopy(base)
    nid = str(max(int(k) for k in cps) + 1)
    nc.set('id', nid)
    sp = nc.find(HH + 'spacing')
    for k in list(sp.attrib):
        sp.set(k, str(value))
    parent = base.getparent()
    parent.append(nc)
    parent.set('itemCnt', str(len(parent)))
    doc.header_dirty = True
    doc.__dict__.pop('_cp_cache', None)
    return nid


MIN_REFIT = -15          # 고친 줄의 자간은 여기까지만 줄이고, 넘치면 줄바꿈 (작년 -38% 같은 값은 글자가 겹쳐 보임)
CONDENSED = -10          # 이보다 자간을 더 줄인 글자 모양은 '한 줄에 넣으려고 줄인 것'으로 본다


def refit_condensed(doc):
    """고친 표 칸 단락에서 작년에 한 줄에 넣으려고 자간을 크게 줄여 둔 글자(예: -38%)를 새 글 길이에 맞춘다.
    새 글이 짧으면 자간을 0 쪽으로 되돌리고, 길어도 MIN_REFIT(-15%)까지만 줄인다. 그래도 넘치면 한글이 줄을 바꾼다.
    고친 단락만 본다: 글을 바꾼 단락은 줄 배치 정보(linesegarray)가 지워져 있다."""
    n = 0
    for root in doc.sections:
        for tc in root.iter(HP + 'tc'):
            sz, mg = tc.find(HP + 'cellSz'), tc.find(HP + 'cellMargin')
            if sz is None:
                continue
            avail = int(sz.get('width', '0')) - (int(mg.get('left', '0')) + int(mg.get('right', '0')) if mg is not None else 0) - 200
            sub = tc.find(HP + 'subList')
            for p in (sub if sub is not None else []):
                if local(p) != 'p' or p.find(HP + 'linesegarray') is not None:
                    continue
                runs = [(r, _run_text(r)) for r in own_runs(p)]
                tight = [(r, t) for r, t in runs if t and _char_props(doc, r.get('charPrIDRef'))[2] <= CONDENSED]
                if not tight:
                    continue
                for r, t in tight:
                    cid = r.get('charPrIDRef')
                    h, ra, s0 = _char_props(doc, cid)
                    rest = sum(text_width(x, *_char_props(doc, rr.get('charPrIDRef'))) for rr, x in runs if rr is not r)
                    # 한글 글자 줄은 MIN_REFIT 까지만 (더 좁으면 겹쳐 보임, 넘치면 줄바꿈).
                    # 숫자 칸("시 △360,000")은 좁은 열에 맞춘 작년 값까지 허용 (숫자가 줄에서 끊기는 것이 더 나쁨)
                    floor = max(s0, MIN_REFIT) if re.search(r'[가-힣]{2,}', t) else s0
                    best = floor                        # 그래도 넘치면 한글이 줄을 바꿈
                    for v in range(0, floor - 1, -5):
                        if rest + text_width(t, h, ra, v) <= avail * 0.92:          # 어림이라 여유를 둠
                            best = v
                            break
                    if best != s0:
                        r.set('charPrIDRef', spacing_variant(doc, cid, best))
                        n += 1
    return n


def set_marked_text(doc, p, text, model=None):
    """**굵게** 표시가 든 글로 단락을 다시 쓴다. 보통 글자는 견본(model, 없으면 p)의 보통 글자 모양,
    굵은 글자는 견본의 굵은 글자 모양 (없으면 보통 글자 모양의 굵은 판을 만든다)."""
    model = model if model is not None else p
    bids = bold_ids(doc)
    runs = [r for r in own_runs(model) if any(local(c) == 't' for c in r)] or own_runs(model)
    plain = next((r.get('charPrIDRef') for r in runs if r.get('charPrIDRef') not in bids and ''.join(
        t.text or '' for t in r.iter(HP + 't')).strip()), None) or (runs[0].get('charPrIDRef') if runs else '0')
    bold = next((r.get('charPrIDRef') for r in runs if r.get('charPrIDRef') in bids), None)
    parts, pos = [], 0
    for m in BOLD_MARK.finditer(text):
        if m.start() > pos:
            parts.append((False, text[pos:m.start()]))
        parts.append((True, m.group(1)))
        pos = m.end()
    if pos < len(text):
        parts.append((False, text[pos:]))
    if any(b for b, _ in parts) and bold is None:
        bold = bold_variant(doc, plain)
    for r in own_runs(p):
        if not any(local(c) in ('secPr', 'ctrl') for c in r):
            p.remove(r)
    ls = p.find(HP + 'linesegarray')
    for b, t in parts:
        if not t:
            continue
        run = etree.Element(HP + 'run')
        run.set('charPrIDRef', bold if b else plain)
        etree.SubElement(run, HP + 't').text = t
        if ls is not None:
            ls.addprevious(run)
        else:
            p.append(run)
    drop_lineseg(p)
    return True


def write_para(doc, p, text, model=None):
    """set_para, insert_para 공통: 견본의 앞 빈칸을 따르고, **굵게** 표시가 있으면 굵게 처리."""
    model_text = para_text(model if model is not None else p)
    text = keep_indent(model_text, text)
    if '**' in text:
        return set_marked_text(doc, p, text, model)
    return set_text(p, text)


# ---------------------------------------------------------------- 문서


class HwpxDoc:
    def __init__(self, path):
        self.path = path
        z = zipfile.ZipFile(path)
        self.infos = z.infolist()
        self.raw = {i.filename: z.read(i.filename) for i in self.infos}
        self.section_names = self._spine()
        self.decl = {}
        self.sections = []
        for n in self.section_names:
            raw = self.raw[n]
            self.decl[n] = raw[:raw.index(b'?>') + 2] if raw.lstrip().startswith(b'<?xml') else b''
            self.sections.append(etree.fromstring(raw))
        self.aliases = {}
        self._header = None
        self.header_dirty = False
        self.reindex()

    def header(self):
        if self._header is None:
            self._header = etree.fromstring(self.raw['Contents/header.xml'])
        return self._header

    def _spine(self):
        hpf = etree.fromstring(self.raw['Contents/content.hpf'])
        items = {it.get('id'): it.get('href') for it in hpf.iter(OPF + 'item')}
        out = []
        for ref in hpf.iter(OPF + 'itemref'):
            href = items.get(ref.get('idref'), '')
            if re.match(r'Contents/section\d+\.xml$', href):
                out.append(href)
        return out

    def reindex(self):
        self.tables = []
        self.table_sec = []
        for si, root in enumerate(self.sections):
            for t in root.iter(HP + 'tbl'):
                self.tables.append(t)
                self.table_sec.append(si)

    # 주소 해석 ---------------------------------------------------
    def para(self, addr):
        if addr in self.aliases:
            return self.aliases[addr]
        m = re.fullmatch(r's(\d+):p(\d+)', addr)
        if not m:
            raise ValueError(f'단락 주소 형식 오류: {addr}')
        root = self.sections[int(m.group(1))]
        kids = [c for c in root if local(c) == 'p']
        return kids[int(m.group(2))]

    def table(self, tid):
        if tid in self.aliases:
            return self.aliases[tid]
        m = re.fullmatch(r't(\d+)', tid)
        if not m:
            raise ValueError(f'표 주소 형식 오류: {tid}')
        return self.tables[int(m.group(1))]

    def cell(self, tid, r, c):
        tc = cells_of(self.table(tid)).get((int(r), int(c)))
        if tc is None:
            raise KeyError(f'{tid}:r{r}:c{c} 칸이 없습니다 (병합 칸이면 시작 위치를 쓰세요)')
        return tc

    def addr_of_para(self, p):
        for si, root in enumerate(self.sections):
            kids = [c for c in root if local(c) == 'p']
            for i, k in enumerate(kids):
                if k is p:
                    return f's{si}:p{i}'
        return None

    def tid_of(self, tbl):
        for i, t in enumerate(self.tables):
            if t is tbl:
                return f't{i}'
        for k, v in self.aliases.items():
            if v is tbl:
                return k
        return None

    def top_paras(self):
        for si, root in enumerate(self.sections):
            for i, p in enumerate([c for c in root if local(c) == 'p']):
                yield si, i, p

    def scope_elements(self, scope):
        """scope: 'all' | 's0:p10-s0:p40' | 's0:p10' | 't12'"""
        if scope in (None, 'all'):
            return list(self.sections)
        if re.fullmatch(r't\d+', scope) or scope in self.aliases and local(self.aliases[scope]) == 'tbl':
            return [self.table(scope)]
        m = re.fullmatch(r'(s\d+:p\d+)(?:-(s\d+:p\d+))?', scope)
        if not m:
            raise ValueError(f'범위 형식 오류: {scope}')
        a = m.group(1)
        b = m.group(2) or a
        out = []
        inside = False
        for si, i, p in self.top_paras():
            ad = f's{si}:p{i}'
            if ad == a:
                inside = True
            if inside:
                out.append(p)
            if ad == b:
                break
        return out

    # 저장 ---------------------------------------------------------
    def save(self, out, title=None):
        new = {}
        for n, root in zip(self.section_names, self.sections):
            new[n] = self.decl[n] + etree.tostring(root, encoding='UTF-8')
        # 섹션 정합성
        hdr = self.raw['Contents/header.xml']
        if self.header_dirty and self._header is not None:
            decl = hdr[:hdr.index(b'?>') + 2] if hdr.lstrip().startswith(b'<?xml') else b''
            hdr = decl + etree.tostring(self._header, encoding='UTF-8')
        hdr = re.sub(rb'secCnt="\d+"', f'secCnt="{len(self.sections)}"'.encode(), hdr, count=1)
        new['Contents/header.xml'] = hdr
        hpf = self.raw['Contents/content.hpf'].decode('utf-8')
        if title:
            hpf = re.sub(r'(<opf:title>)(.*?)(</opf:title>)', lambda m: m.group(1) + title + m.group(3), hpf, count=1)
        new['Contents/content.hpf'] = hpf.encode('utf-8')
        # 미리보기 글
        if 'Preview/PrvText.txt' in self.raw:
            txt = []
            for root in self.sections:
                for p in root.iter(HP + 'p'):
                    if p.find('.//' + HP + 'p') is None:
                        s = para_text(p).strip()
                        if s:
                            txt.append(s)
                if sum(len(x) for x in txt) > 1000:
                    break
            new['Preview/PrvText.txt'] = '\r\n'.join(txt)[:1024].encode('utf-8')
        with zipfile.ZipFile(out, 'w') as zo:
            for info in self.infos:
                zi = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                zi.compress_type = info.compress_type
                zi.external_attr = info.external_attr
                zi.create_system = info.create_system
                zo.writestr(zi, new.get(info.filename, self.raw[info.filename]))
        return out


# ---------------------------------------------------------------- 행 편집


def grid(tbl):
    rows = tbl_rows(tbl)
    return rows, [[tc for tc in tr if local(tc) == 'tc'] for tr in rows]


def renumber_rows(tbl):
    rows = tbl_rows(tbl)
    for ri, tr in enumerate(rows):
        for tc in tr:
            if local(tc) == 'tc':
                set_addr(tc, r=ri)
    tbl.set('rowCnt', str(len(rows)))


def row_height(tr):
    hs = [cell_height(tc) for tc in tr if local(tc) == 'tc' and cell_info(tc)[2] == 1]
    return max(hs) if hs else 0


def unmerge_rows(tbl, r0, r1):
    """[r0, r1) 행 범위 안의 세로 병합을 풀어 각 행에 칸을 만든다."""
    rows = tbl_rows(tbl)
    for ri in range(0, r0):
        for tc in [x for x in rows[ri] if local(x) == 'tc']:
            r, c, rs, cs = cell_info(tc)
            if r + rs > r0:
                raise ValueError(f'편집 범위 위쪽 칸 r{r}c{c}가 범위 안까지 병합되어 있습니다. data_start를 더 위로 잡으세요')
    for ri in range(r0, r1):
        tr = rows[ri]
        for tc in [x for x in tr if local(x) == 'tc']:
            r, c, rs, cs = cell_info(tc)
            if rs <= 1:
                continue
            if r + rs > r1:
                raise ValueError(f'병합 칸 r{r}c{c}가 편집 범위({r0}~{r1 - 1}) 밖까지 이어집니다. data_end를 늘리세요')
            total = cell_height(tc)
            each = max(1, total // rs)
            set_span(tc, rs=1)
            set_cell_height(tc, each)
            for k in range(1, rs):
                clone = copy.deepcopy(tc)
                strip_fields(clone)
                clear_cell(clone)
                set_addr(clone, r=r + k)
                target = rows[r + k]
                placed = False
                for other in [x for x in target if local(x) == 'tc']:
                    if cell_info(other)[1] > c:
                        other.addprevious(clone)
                        placed = True
                        break
                if not placed:
                    target.append(clone)


def merge_runs(tbl, r0, r1, col):
    """[r0, r1) 범위에서 col 열의 같은 글자가 이어지면 세로 병합."""
    rows = tbl_rows(tbl)
    def cell_at(ri):
        for tc in rows[ri]:
            if local(tc) == 'tc' and cell_info(tc)[1] == col:
                return tc
        return None
    ri = r0
    while ri < r1:
        head = cell_at(ri)
        if head is None:
            ri += 1
            continue
        key = re.sub(r'\s+', '', cell_text(head))
        rj = ri + 1
        while rj < r1:
            nxt = cell_at(rj)
            if nxt is None or re.sub(r'\s+', '', cell_text(nxt)) != key or not key:
                break
            rj += 1
        if rj - ri > 1:
            h = cell_height(head)
            for k in range(ri + 1, rj):
                c2 = cell_at(k)
                h += cell_height(c2)
                rows[k].remove(c2)
            set_span(head, rs=rj - ri)
            set_cell_height(head, h)
        ri = rj


def _fill_tr(tr, vals):
    tcs = [x for x in tr if local(x) == 'tc']
    if isinstance(vals, dict):
        for tc in tcs:
            c = cell_info(tc)[1]
            v = vals.get(str(c), vals.get(c))
            set_cell_text(tc, '' if v is None else str(v))
    else:
        if len(vals) != len(tcs):
            raise ValueError(f'값 {len(vals)}개가 행의 칸 수 {len(tcs)}와 다릅니다. 이 행의 칸 열: {[cell_info(x)[1] for x in tcs]}')
        for tc, v in zip(tcs, vals):
            set_cell_text(tc, '' if v is None else str(v))
    for p in tr.iter(HP + 'p'):
        drop_lineseg(p)


def merge_group(tbl, r0, size, col):
    """r0부터 size행의 col 열을 하나로 병합 (첫 칸 글자 유지)."""
    rows = tbl_rows(tbl)
    def cell_at(ri):
        for tc in rows[ri]:
            if local(tc) == 'tc' and cell_info(tc)[1] == col:
                return tc
        return None
    head = cell_at(r0)
    if head is None:
        return
    h = cell_height(head)
    for k in range(r0 + 1, r0 + size):
        c2 = cell_at(k)
        if c2 is not None:
            h += cell_height(c2)
            rows[k].remove(c2)
    set_span(head, rs=size)
    set_cell_height(head, h)


def fill_rows(tbl, rows_values, data_start, data_end=None, template_row=None, merge_cols=(),
              group_size=1, group_merge_cols=()):
    """data_start ~ data_end-1 행을 비우고 rows_values 항목마다 template_row(묶음이면 group_size행)를 복제해 채움.

    rows_values: 묶음이 1행이면 [칸값, ...] 또는 {"열번호": 값} 의 목록,
                 묶음이 k행이면 [[1행 칸값...], [2행 칸값...], ...] 의 목록.
    병합을 먼저 풀기 때문에, 병합 칸이 있던 자리도 칸 하나로 셉니다 (table 명령으로 확인).
    """
    rows = tbl_rows(tbl)
    n = len(rows)
    if data_end is None:
        data_end = n
    data_end = int(data_end)
    if data_end < 0:
        data_end = n + data_end
    data_start = int(data_start)
    template_row = data_start if template_row is None else int(template_row)
    group_size = int(group_size)
    if not (0 <= data_start <= template_row and template_row + group_size <= data_end <= n):
        raise ValueError(f'행 범위 오류: data_start={data_start} template_row={template_row} group_size={group_size} data_end={data_end} (행 {n}개)')
    unmerge_rows(tbl, data_start, data_end)
    rows = tbl_rows(tbl)
    tmpl = rows[template_row:template_row + group_size]
    region = rows[data_start:data_end]
    anchor_prev = rows[data_start - 1] if data_start > 0 else None
    new_trs = []
    for item in rows_values:
        group_vals = [item] if group_size == 1 else item
        if len(group_vals) != group_size:
            raise ValueError(f'묶음 {group_size}행인데 값은 {len(group_vals)}행입니다: {str(item)[:80]}')
        for t_tr, vals in zip(tmpl, group_vals):
            tr = copy.deepcopy(t_tr)
            strip_fields(tr)
            _fill_tr(tr, vals)
            new_trs.append(tr)
    for tr in region:
        tbl.remove(tr)
    if anchor_prev is not None:
        pos = anchor_prev
        for tr in new_trs:
            pos.addnext(tr)
            pos = tr
    else:
        remaining = tbl_rows(tbl)
        if remaining:
            first = remaining[0]
            for tr in reversed(new_trs):
                first.addprevious(tr)
                first = tr
        else:
            for tr in new_trs:
                tbl.append(tr)
    renumber_rows(tbl)
    if group_size > 1:
        for gi in range(len(rows_values)):
            for col in group_merge_cols:
                merge_group(tbl, data_start + gi * group_size, group_size, int(col))
    for col in merge_cols:
        merge_runs(tbl, data_start, data_start + len(new_trs), int(col))
    fix_table_height(tbl)
    return len(rows_values)


def delete_rows(tbl, r0, r1):
    unmerge_rows(tbl, r0, r1)
    rows = tbl_rows(tbl)
    for tr in rows[r0:r1]:
        tbl.remove(tr)
    renumber_rows(tbl)
    fix_table_height(tbl)


def fix_table_height(tbl):
    sz = tbl.find(HP + 'sz')
    if sz is None:
        return
    total = sum(row_height(tr) for tr in tbl_rows(tbl))
    if total:
        sz.set('height', str(total))


def holder_para(el):
    """요소를 직접 담고 있는 가장 가까운 단락."""
    for a in el.iterancestors():
        if local(a) == 'p':
            return a
    return None


def is_nested_table(t):
    return any(local(x) == 'tbl' for x in t.iterancestors())


def containing_top_para(el):
    p = None
    for a in el.iterancestors():
        if local(a) == 'p':
            p = a
        if local(a) == 'sec':
            break
    return p




# ---------------------------------------------------------------- 비교표, 금액 문장


def _ka():
    import importlib
    sys.path.insert(0, __import__('os').path.dirname(__import__('os').path.abspath(__file__)))
    return importlib.import_module('korean_amount')


def header_cols(tbl):
    """머리 행에서 열 역할 찾기: cur, prev, diff, rate, page. (연도가 가장 큰 열 = cur)"""
    rows = tbl_rows(tbl)
    best = None
    for ri, tr in enumerate(rows[:5]):
        names = {}
        for tc in tr:
            if local(tc) == 'tc':
                names[cell_info(tc)[1]] = re.sub(r'\s+', '', cell_text(tc))
        joined = '|'.join(names.values())
        if re.search(r'(증감|기정|본예산|요구액|예산액)', joined):
            best = (ri, names)
    if best is None:
        return None, {}
    ri, names = best
    years = sorted({int(y) for n in names.values() for y in re.findall(r'(?<!\d)(20\d\d)(?!\d)', n)})
    roles = {}
    for c, n in sorted(names.items()):
        if 'cur' not in roles and ((years and str(years[-1]) in n and '이후' not in n) or n in ('예산액', '금회')):
            roles['cur'] = c
        elif 'prev' not in roles and ((len(years) > 1 and str(years[-2]) in n) or '기정' in n or '전년' in n):
            roles['prev'] = c
        elif 'diff' not in roles and n.startswith('증감') and '률' not in n or n in ('비교증감',):
            roles.setdefault('diff', c)
        elif 'rate' not in roles and ('률' in n or n == '%'):
            roles['rate'] = c
        elif 'page' not in roles and '페이지' in n:
            roles['page'] = c
    return ri, roles


def _split_amount_lines(text):
    """'2,329,350\n시 2,329,350' -> [('', '2,329,350'), ('시', '2,329,350')]"""
    out = []
    for line in str(text).split('\n'):
        m = re.search(r'△?-?\d[\d,]*', line)
        if not m:
            out.append((line, None, line))
            continue
        out.append((line[:m.start()], m.group(0), line))
    return out


def _num(tok):
    tok = tok.replace(',', '')
    neg = tok.startswith('△') or tok.startswith('-')
    v = int(tok.lstrip('△-') or 0)
    return -v if neg else v


def compare_values(cur, prev, rate_digits=2, rate_mode='round', zero='-'):
    ka = _ka()
    cl, pl = _split_amount_lines(cur), _split_amount_lines(prev)
    pmap = {}
    for lab, tok, _ in pl:
        if tok is not None:
            pmap.setdefault(lab.strip(), tok)
    diff_lines = []
    first = None
    for lab, tok, raw in cl:
        if tok is None:
            diff_lines.append(raw if raw.strip() == '' else '')
            continue
        key = lab.strip()
        p = pmap.get(key)
        if p is None and key == '' and pl:
            p = next((t for l, t, _ in pl if t is not None), None)
        if p is None:
            diff_lines.append(lab + '-')
            continue
        d = _num(tok) - _num(p)
        if first is None:
            first = (d, _num(p))
        diff_lines.append(lab + (ka.fmt_num(d) if d != 0 else zero))
    rate = ''
    if first is not None:
        rate = ka.rate(first[0], first[1], rate_digits, rate_mode) if first[1] else zero
        if first[0] == 0 and first[1]:
            rate = zero
    return '\n'.join(diff_lines), rate


# ---------------------------------------------------------------- 연도 이동

def year_shift_text(s, delta, lo=1990, hi=2100):
    def y4(m):
        y = int(m.group(0))
        return str(y + delta) if lo <= y <= hi else m.group(0)
    s = re.sub(r'(?<![\d,.])(19|20)\d\d(?![\d,])', y4, s)

    def y2(m):
        yy = int(m.group(2))
        return f'{m.group(1)}{(yy + delta) % 100:02d}'
    s = re.sub(r"([`‘’'´])(\d\d)(?=[.년\s])", y2, s)
    return s


# ---------------------------------------------------------------- 계획 적용


def apply_plan(doc, plan, log=print):
    ops = plan['ops'] if isinstance(plan, dict) else plan
    # 1) 모든 주소를 먼저 요소로 바꿔 둔다 (구조 변경으로 번호가 밀려도 안전)
    resolved = []
    for i, op in enumerate(ops):
        r = dict(op)
        try:
            if 'table' in op and not str(op['table']).startswith('@'):
                r['_table'] = doc.table(op['table'])
            if 'para' in op and not str(op['para']).startswith('@'):
                r['_para'] = doc.para(op['para'])
            if 'after' in op and not str(op['after']).startswith('@'):
                r['_after'] = doc.para(op['after'])
            if 'source' in op and not str(op['source']).startswith('@'):
                r['_source'] = doc.table(op['source']) if op['source'].startswith('t') else doc.para(op['source'])
            if 'like' in op and not str(op['like']).startswith('@'):
                r['_like'] = doc.para(op['like'])
            if op.get('op') in ('replace', 'year_shift', 'replace_number'):
                r['_scope'] = doc.scope_elements(op.get('scope', 'all'))
            # set_cell, set_cells, set_compare_row 의 행과 열 번호는 적용하는 때의 표 기준
            # (같은 계획에서 앞의 fill_rows 로 행이 바뀌었으면 바뀐 표의 번호)
        except Exception as e:
            raise SystemExit(f'[계획 {i}] 주소 해석 실패: {op} -> {e}')
        resolved.append(r)

    def get(r, key, kind):
        v = r.get('_' + key)
        if v is not None:
            return v
        name = r.get(key)
        if name in doc.aliases:
            return doc.aliases[name]
        raise SystemExit(f'별칭 {name} 이(가) 아직 정의되지 않았습니다')

    stats = {}
    last_after = {}
    keep = []                       # id() 가 안정적이도록 기준 단락 참조를 붙잡아 둠
    for r in resolved:
        if r.get('_after') is not None:
            keep.append(r['_after'])
    for i, r in enumerate(resolved):
        op = r['op']
        stats[op] = stats.get(op, 0) + 1
        try:
            if op in ('set_cell', 'set_cells'):
                tbl = get(r, 'table', 't')
                cmap = cells_of(tbl)
                items = [(r['row'], r['col'], r['text'])] if op == 'set_cell' else r['cells']
                for row, col, text in items:
                    tc = cmap.get((int(row), int(col)))
                    if tc is None:
                        rows_n = len(tbl_rows(tbl))
                        raise KeyError(f"{r['table']}:r{row}:c{col} 칸 없음 (지금 표는 {rows_n}행. 병합 칸은 시작 위치, 앞 연산이 행을 바꿨으면 바뀐 번호)")
                    set_cell_text(tc, text)
            elif op == 'set_para':
                write_para(doc, get(r, 'para', 'p'), r['text'])
            elif op == 'append_text':
                p = get(r, 'para', 'p')
                set_text(p, para_text(p) + r['text'])
            elif op == 'replace':
                n = 0
                for el in r['_scope']:
                    for p in el.iter(HP + 'p'):
                        segs_txt = para_text(p)
                        if r.get('regex'):
                            new = re.sub(r['find'], r['replace'], segs_txt)
                        else:
                            new = segs_txt.replace(r['find'], r['replace'])
                        if new != segs_txt:
                            set_text(p, new)
                            n += 1
                log(f'  replace "{r["find"]}" -> {n}개 단락')
            elif op == 'year_shift':
                n = 0
                only = r.get('only', 'headers')
                targets = []
                for el in r['_scope']:
                    if only == 'all':
                        targets += list(el.iter(HP + 'p'))
                        continue
                    roots = [el] if local(el) in ('p', 'tbl') else [c for c in el if local(c) == 'p']
                    for root in roots:
                        tops = [root] if local(root) == 'p' else []
                        for p in tops:
                            # 표 밖 짧은 단락 (제목, 소제목)
                            if len(para_text(p).strip()) <= int(r.get('max_len', 60)):
                                targets.append(p)
                        for t in root.iter(HP + 'tbl'):
                            rc, cc = int(t.get('rowCnt')), int(t.get('colCnt'))
                            if rc * cc == 1:
                                targets += list(t.iter(HP + 'p'))
                                continue
                            hi, _ = header_cols(t)
                            rows = tbl_rows(t)
                            for tr in rows[:(hi + 1 if hi is not None else 1)]:
                                targets += list(tr.iter(HP + 'p'))
                seen = set()
                for p in targets:
                    if id(p) in seen:
                        continue
                    seen.add(id(p))
                    s_ = para_text(p)
                    ns = year_shift_text(s_, int(r.get('delta', 1)))
                    if ns != s_:
                        set_text(p, ns)
                        n += 1
                log(f'  year_shift {r.get("delta", 1):+d} ({only}) -> {n}개 단락')
            elif op == 'set_compare_row':
                tbl = get(r, 'table', 't')
                hi, roles = header_cols(tbl)
                roles.update({k: int(v) for k, v in (r.get('cols') or {}).items()})
                cmap = cells_of(tbl)
                row = int(r['row'])
                need = ['cur', 'prev']
                for k in need:
                    if k not in roles:
                        raise ValueError(f'열 역할 {k}를 찾지 못함. "cols": {{"cur":1,"prev":2,"diff":3,"rate":4}} 로 지정하세요')
                defaults = plan.get('defaults', {}) if isinstance(plan, dict) else {}
                diff, rate_s = compare_values(r['cur'], r['prev'], int(r.get('rate_digits', defaults.get('rate_digits', 2))),
                                              r.get('rate_mode', defaults.get('rate_mode', 'round')), r.get('zero', defaults.get('zero', '-')))
                vals = {'cur': r['cur'], 'prev': r['prev'], 'diff': diff, 'rate': rate_s}
                if 'page' in r:
                    vals['page'] = str(r['page'])
                if 'label' in r and r.get('label_col') is not None:
                    roles['label'] = int(r['label_col'])
                    vals['label'] = r['label']
                for k, v in vals.items():
                    c = roles.get(k)
                    if c is None:
                        continue
                    tc = cmap.get((row, c))
                    if tc is not None:
                        if k == 'rate' and re.search(r'\d', v or ''):
                            old_v = cell_text(tc).strip()
                            if old_v.endswith('%') and not v.endswith('%'):
                                v += '%'        # 작년 칸에 % 가 붙어 있었으면 그대로
                        set_cell_text(tc, v)
                for k, v in (r.get('extra') or {}).items():
                    tc = cmap.get((row, int(k)))
                    if tc is not None:
                        set_cell_text(tc, v)
            elif op == 'rewrite_sentence':
                ka = _ka()
                p = get(r, 'para', 'p')
                old_s = para_text(p)
                defaults = plan.get('defaults', {}) if isinstance(plan, dict) else {}
                new_s, why = ka.rewrite_sentence(old_s, r['cur'], r['prev'], r.get('unit', '천원'),
                                                 r.get('digits'), r.get('rate_mode', defaults.get('rate_mode', 'round')))
                if new_s is None:
                    if r.get('text_if_zero') and '0' in (why or ''):
                        set_text(p, r['text_if_zero'])
                    else:
                        raise ValueError(f'문장을 다시 쓰지 못함 ({why}): {old_s[:80]}')
                else:
                    set_text(p, new_s)
            elif op == 'fill_rows':
                tbl = get(r, 'table', 't')
                k = fill_rows(tbl, r['rows'], r['data_start'], r.get('data_end'),
                              r.get('template_row'), r.get('merge_cols', ()),
                              r.get('group_size', 1), r.get('group_merge_cols', ()))
                log(f'  fill_rows {r["table"]} -> 데이터 행 {k}개')
            elif op == 'delete_rows':
                delete_rows(get(r, 'table', 't'), int(r['start']), int(r['end']))
            elif op == 'delete_table':
                tbl = get(r, 'table', 't')
                p = holder_para(tbl)
                parent = p.getparent() if p is not None else None
                only_this = p is not None and not para_text(p).strip() and len(list(p.iter(HP + 'tbl'))) == len(list(tbl.iter(HP + 'tbl')))
                if only_this and not (local(parent) == 'subList' and len([x for x in parent if local(x) == 'p']) == 1):
                    parent.remove(p)
                else:
                    tbl.getparent().remove(tbl)
            elif op == 'shift_cols':
                # 연도별 표: 이전 연도 값을 오른쪽 열로 옮김 (map: {원래 열: 옮길 열}). 모든 데이터 행에 적용.
                tbl = get(r, 'table', 't')
                cmap = cells_of(tbl)
                ds = int(r.get('data_start', 1))
                mp = {int(k): int(v) for k, v in r['map'].items()}
                rows_n = int(tbl.get('rowCnt'))
                snap = {(row, c): cell_text(cmap[(row, c)]) for row in range(ds, rows_n) for c in mp if (row, c) in cmap}
                for (row, c), text in snap.items():
                    tc = cmap.get((row, mp[c]))
                    if tc is not None:
                        set_cell_text(tc, text)
                for c in r.get('clear', []):
                    for row in range(ds, rows_n):
                        if (row, int(c)) in cmap:
                            set_cell_text(cmap[(row, int(c))], '')
            elif op == 'page_break':
                get(r, 'para', 'p').set('pageBreak', '1' if r.get('on', True) else '0')
            elif op == 'delete_para':
                p = get(r, 'para', 'p')
                p.getparent().remove(p)
            elif op == 'copy_table':
                src = get(r, 'source', 't')
                sp = holder_para(src)
                newp = copy.deepcopy(sp)
                newp.set('pageBreak', '0')
                strip_fields(newp)
                for t in newp.iter(HP + 'tbl'):
                    t.set('id', new_id())
                after = get(r, 'after', 'p')
                after = last_after.get(id(after), after)    # 같은 단락 뒤에 여러 번 넣으면 계획 순서대로 이어 붙임
                after.addnext(newp)
                last_after[id(get(r, 'after', 'p'))] = newp
                newtbl = next(newp.iter(HP + 'tbl'))
                if r.get('as'):
                    doc.aliases[r['as']] = newtbl
                    doc.aliases[r['as'] + ':para'] = newp
            elif op == 'insert_para':
                like = get(r, 'like', 'p')
                newp = copy.deepcopy(like)
                strip_fields(newp)
                for t in list(newp.iter(HP + 'tbl')):
                    t.getparent().remove(t)
                newp.set('pageBreak', '0')
                write_para(doc, newp, r.get('text', ''), model=like)
                anchor = get(r, 'after', 'p')
                after = last_after.get(id(anchor), anchor)  # 같은 단락 뒤에 여러 번 넣으면 계획 순서대로 이어 붙임
                after.addnext(newp)
                last_after[id(anchor)] = newp
                if r.get('as'):
                    doc.aliases[r['as']] = newp
            else:
                raise SystemExit(f'[계획 {i}] 알 수 없는 연산: {op}')
        except SystemExit:
            raise
        except Exception as e:
            raise SystemExit(f'[계획 {i}] {op} 실패: {e}\n  연산: {json.dumps({k: v for k, v in r.items() if not k.startswith("_")}, ensure_ascii=False)[:400]}')
    if stats:
        stats['검토 필요 빨간 글자'] = color_review_marks(doc)
        stats['자간 맞춤'] = refit_condensed(doc)
        # 표를 지우거나 행, 단락을 넣고 빼면 뒤 단락들의 저장된 줄 위치(vertpos)가 낡는다.
        # 그대로 두면 그 위치를 믿는 보기 프로그램에서 빈 공간이나 겹침이 생기므로 본문 단락의 줄 배치 정보를 지워
        # 한글이 다시 계산하게 한다 (표 칸 안 단락은 그대로).
        for root in doc.sections:
            for p in root:
                if local(p) == 'p':
                    drop_lineseg(p)
    doc.reindex()
    return stats


# ---------------------------------------------------------------- 보기


def short(s, n):
    s = s.replace('\n', ' / ')
    return s if len(s) <= n else s[:n - 1] + '…'


def table_lines(doc, tbl, full=False, indent='  '):
    tid = doc.tid_of(tbl) or '?'
    rows = tbl_rows(tbl)
    out = [f'{indent}[{tid}] 표 {tbl.get("rowCnt")}x{tbl.get("colCnt")}']
    w = 400 if full else 60
    for tr in rows:
        cells = []
        for tc in tr:
            if local(tc) != 'tc':
                continue
            r, c, rs, cs = cell_info(tc)
            span = ''
            if rs > 1 or cs > 1:
                span = f'({rs}x{cs})'
            txt = cell_text(tc)
            nested = list(tc.iter(HP + 'tbl'))
            if nested:
                txt += ' {중첩표 ' + ', '.join(doc.tid_of(t) or '?' for t in nested) + '}'
            cells.append(f'c{c}{span}={short(txt, w)!r}')
        r0 = cell_info([x for x in tr if local(x) == 'tc'][0])[0] if len(tr) else '?'
        out.append(f'{indent}  r{r0}: ' + ' | '.join(cells))
    return out


def dump(doc, a=None, b=None, full=False):
    inside = a is None
    for si, i, p in doc.top_paras():
        ad = f's{si}:p{i}'
        if ad == a:
            inside = True
        if inside:
            txt = para_text(p)
            tbls = [t for t in p.iter(HP + 'tbl') if holder_para(t) is p]
            flag = ' (쪽나눔)' if p.get('pageBreak') == '1' else ''
            if txt.strip():
                print(f'[{ad}]{flag} {short(txt, 400 if full else 160)}' + (' (표가 이 단락 안에 있음)' if tbls else ''))
            elif tbls:
                print(f'[{ad}]{flag} (표만 있는 단락)')
            else:
                print(f'[{ad}]{flag}')
            for t in tbls:
                for line in table_lines(doc, t, full):
                    print(line)
                for nt in [x for x in t.iter(HP + 'tbl') if x is not t]:
                    for line in table_lines(doc, nt, full, indent='      '):
                        print(line)
        if ad == b:
            break


def outline(doc):
    """블록을 나눌 때 쓰는 제목 후보: 1칸짜리 표(제목 상자)와 머리말 단락."""
    head_re = re.compile(r'^\s*([ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]\.?|\d{1,2}\.|[가-하]\.|\(\d{1,2}\)|\d\)|□|■|◈|【|<)')
    for si, i, p in doc.top_paras():
        ad = f's{si}:p{i}'
        tbls = [t for t in p.iter(HP + 'tbl') if not any(local(x) == 'tbl' for x in t.iterancestors())]
        for t in tbls:
            rc, cc = int(t.get('rowCnt')), int(t.get('colCnt'))
            if rc * cc == 1:
                print(f'[{ad}] 제목상자 {doc.tid_of(t)}: {short(cell_text(next(t.iter(HP + "tc"))), 80)}')
            else:
                hdr = ' | '.join(short(cell_text(tc), 14) for tc in list(t.iter(HP + 'tc'))[:6])
                print(f'[{ad}]   표 {doc.tid_of(t)} {rc}x{cc}: {hdr}')
        txt = para_text(p).strip()
        if txt and head_re.match(txt) and len(txt) < 80:
            print(f'[{ad}] {txt}')


def main():
    try:
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    for name in ('info', 'outline', 'text'):
        s = sub.add_parser(name)
        s.add_argument('doc')
    s = sub.add_parser('dump')
    s.add_argument('doc')
    s.add_argument('--from', dest='a')
    s.add_argument('--to', dest='b')
    s.add_argument('--full', action='store_true')
    s = sub.add_parser('table')
    s.add_argument('doc')
    s.add_argument('tid')
    s.add_argument('--full', action='store_true')
    s = sub.add_parser('find')
    s.add_argument('doc')
    s.add_argument('query')
    s = sub.add_parser('apply')
    s.add_argument('doc')
    s.add_argument('plan')
    s.add_argument('-o', '--out', required=True)
    s.add_argument('--title')
    a = ap.parse_args()
    doc = HwpxDoc(a.doc)
    if a.cmd == 'info':
        print(json.dumps({'sections': doc.section_names,
                          'paragraphs': [len([c for c in r if local(c) == 'p']) for r in doc.sections],
                          'tables': len(doc.tables)}, ensure_ascii=False, indent=1))
    elif a.cmd == 'outline':
        outline(doc)
    elif a.cmd == 'text':
        for si, i, p in doc.top_paras():
            for el in p.iter(HP + 'p'):
                if el.find('.//' + HP + 'p') is None:
                    s = para_text(el)
                    if s.strip():
                        print(s)
    elif a.cmd == 'dump':
        dump(doc, a.a, a.b, a.full)
    elif a.cmd == 'table':
        for line in table_lines(doc, doc.table(a.tid), a.full, indent=''):
            print(line)
    elif a.cmd == 'find':
        q = re.sub(r'\s+', '', a.query)
        for si, i, p in doc.top_paras():
            if q in re.sub(r'\s+', '', para_text(p)):
                print(f's{si}:p{i}  {short(para_text(p), 120)}')
            for t in p.iter(HP + 'tbl'):
                for (r, c), tc in cells_of(t).items():
                    if q in re.sub(r'\s+', '', cell_text(tc)):
                        print(f'{doc.tid_of(t)}:r{r}:c{c}  (s{si}:p{i})  {short(cell_text(tc), 100)}')
    elif a.cmd == 'apply':
        plan = json.load(open(a.plan, encoding='utf-8'))
        stats = apply_plan(doc, plan)
        doc.save(a.out, title=a.title)
        print('적용:', json.dumps(stats, ensure_ascii=False), '->', a.out)


if __name__ == '__main__':
    main()
