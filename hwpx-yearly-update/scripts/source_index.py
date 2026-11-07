#!/usr/bin/env python3
"""원자료 색인: 여러 PDF와 보조 자료를 쪽 단위 글로 바꿔 두고 찾아 쓰기.

  python3 source_index.py build IDX 예산서_1.pdf 예산서_2.pdf ... [--aux 설명자료.pdf 설명.hwpx ...]
  python3 source_index.py toc IDX                      # 목차에서 장 이름과 인쇄 쪽 범위 (목차가 없으면 구분 쪽과 쪽 머리글로)
  python3 source_index.py toc IDX --key 부서            # 쪽 머리글 '부서:' 값별 쪽 범위
  python3 source_index.py search IDX "온산수질" [--range 937-950] [--aux]
  python3 source_index.py search IDX 1,860,185          # 숫자 찾기 (쉼표 무시)
  python3 source_index.py pages IDX 939-941             # 인쇄 쪽번호 기준으로 글 보기 (833,867,901 처럼 여러 쪽도)
  python3 source_index.py pages IDX 939-941 --pdf      # PDF 쪽번호(전체 기준)로 보기
  python3 source_index.py cut IDX 자른것 --match 복지보훈여성국 계속비   # 목차 범위대로 PDF 자르기 (쪽 복사)
  python3 source_index.py cut IDX 자른것 --level 1      # 큰 장과 국(실, 사업소, 특별회계)마다 하나씩
  python3 source_index.py cut IDX 자른것 --within 문화복지환경위원회   # 위원회 소관 국, 사업소마다 하나씩

색인 폴더(IDX)에는 pages.jsonl, toc.json, meta.json 이 생긴다.
- 원자료 PDF는 넣은 순서대로 이어 붙여 전체 쪽번호(g)를 매긴다.
- 쪽 아래쪽의 '- 941 -' 같은 인쇄 쪽번호를 읽어 printed 에 적는다.
- --aux 뒤의 보조 자료(설명자료 등)는 따로 표시되어 원자료와 섞이지 않는다.
"""
import argparse
import signal
import gc
import json
import os
import re
import subprocess
import sys

sys.dont_write_bytecode = True     # skill 폴더에 __pycache__ 를 만들지 않음

KORDOC = os.environ.get('KORDOC', 'kordoc@4.24.37')


def pdf_pages(path):
    import pdfplumber
    with pdfplumber.open(path) as pdf:
        n = len(pdf.pages)
    for s in range(0, n, 40):
        with pdfplumber.open(path, pages=list(range(s + 1, min(n, s + 40) + 1))) as pdf:
            for pg in pdf.pages:
                yield pg.extract_text() or ''
                pg.flush_cache()
        gc.collect()


PRINTED_PATTERNS = [re.compile(r'^\s*[-–]\s*(\d{1,4})\s*[-–]\s*$'),
                    re.compile(r'^\s*(\d{1,4})\s*$'),
                    re.compile(r'^\s*(\d{1,4})\s*/\s*\d{1,4}\s*$')]


def printed_number(text):
    lines = [l for l in text.strip().splitlines() if l.strip()]
    for pat in PRINTED_PATTERNS:
        for l in reversed(lines[-3:]):
            m = pat.match(l)
            if m:
                return int(m.group(1))
    return None


def other_to_text(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in ('.txt', '.md', '.csv'):
        return open(path, encoding='utf-8', errors='replace').read()
    if ext == '.hwpx':
        try:
            sys.path.insert(0, os.path.dirname(__file__))
            import hwpx_ops as H
            d = H.HwpxDoc(path)
            out = []
            for root in d.sections:
                for p in root.iter(H.HP + 'p'):
                    if p.find('.//' + H.HP + 'p') is None:
                        t = H.para_text(p)
                        if t.strip():
                            out.append(t)
            return '\n'.join(out)
        except Exception:  # noqa: BLE001
            pass
    try:
        r = subprocess.run(['npx', '-y', KORDOC, path], capture_output=True, text=True, timeout=600)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout
        sys.stderr.write(r.stderr[-500:])
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f'kordoc 실행 실패: {e}\n')
    return ''


def build(idx, files, aux):
    os.makedirs(idx, exist_ok=True)
    out = open(os.path.join(idx, 'pages.jsonl'), 'w', encoding='utf-8')
    g = 0
    meta = {'sources': [], 'aux': []}
    for f in files:
        ext = os.path.splitext(f)[1].lower()
        start = g + 1
        if ext == '.pdf':
            for i, t in enumerate(pdf_pages(f), 1):
                g += 1
                rec = {'kind': 'src', 'file': os.path.basename(f), 'page': i, 'g': g, 'printed': printed_number(t), 'text': t}
                out.write(json.dumps(rec, ensure_ascii=False) + '\n')
        else:
            t = other_to_text(f)
            g += 1
            out.write(json.dumps({'kind': 'src', 'file': os.path.basename(f), 'page': 1, 'g': g, 'printed': None, 'text': t}, ensure_ascii=False) + '\n')
        meta['sources'].append({'file': os.path.abspath(f), 'g_from': start, 'g_to': g})
        print(f'원자료 {f}: 전체 {start}~{g}쪽', file=sys.stderr)
    for f in aux:
        ext = os.path.splitext(f)[1].lower()
        if ext == '.pdf':
            for i, t in enumerate(pdf_pages(f), 1):
                out.write(json.dumps({'kind': 'aux', 'file': os.path.basename(f), 'page': i, 'g': None, 'printed': printed_number(t), 'text': t}, ensure_ascii=False) + '\n')
        else:
            t = other_to_text(f)
            out.write(json.dumps({'kind': 'aux', 'file': os.path.basename(f), 'page': 1, 'g': None, 'printed': None, 'text': t}, ensure_ascii=False) + '\n')
        meta['aux'].append(os.path.abspath(f))
        print(f'보조 자료 {f}', file=sys.stderr)
    out.close()
    recs = list(load(idx))
    src = [r for r in recs if r['kind'] == 'src']
    have = sum(1 for r in src if r['printed'] is not None)
    offs = {}
    for r in src:
        if r['printed'] is not None:
            offs[r['g'] - r['printed']] = offs.get(r['g'] - r['printed'], 0) + 1
    meta['printed_found'] = f'{have}/{len(src)}'
    if offs:
        best = max(offs, key=offs.get)
        meta['g_minus_printed'] = best
        meta['offset_share'] = round(offs[best] / max(1, have), 3)
    toc = parse_toc(src)
    json.dump(toc, open(os.path.join(idx, 'toc.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    json.dump(meta, open(os.path.join(idx, 'meta.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print(json.dumps(meta, ensure_ascii=False, indent=1))
    print(f'목차 항목 {len(toc)}개 (toc 명령으로 보기)')


def load(idx):
    with open(os.path.join(idx, 'pages.jsonl'), encoding='utf-8') as f:
        for line in f:
            yield json.loads(line)


TOC_RE = re.compile(r'([^\d·…\.\s][^·…]*?)\s*[·…\.]{3,}\s*(\d{1,4})\b')


def parse_toc(src, max_pages=20):
    entries = []
    for r in src[:max_pages]:
        t = r['text']
        if len(re.findall(r'[·…]{5,}|\.{8,}', t)) < 3:
            continue
        for line in t.splitlines():
            for m in TOC_RE.finditer(line):
                raw = m.group(1)
                lead = re.match(r'^\s*([○▪□■◦∙•▶【\-])?', raw).group(1) or ''
                level = {'【': 0, '○': 1, '□': 1, '■': 1, '▶': 2, '▪': 2, '◦': 2, '∙': 2, '•': 2, '-': 3}.get(lead, 1)
                title = re.sub(r'^[○▪□■◦∙•▶\-\s]+', '', raw).strip()
                title = re.sub(r'\s+', ' ', title)
                if title:
                    entries.append({'title': title, 'title_nospace': title.replace(' ', ''), 'level': level,
                                    'printed': int(m.group(2)), 'toc_g': r['g']})
    # 쪽 순서가 거꾸로 가는 항목 표시 (목차 오타 가능성)
    for i, e in enumerate(entries):
        prev = [x['printed'] for x in entries[max(0, i - 3):i]]
        nxt = [x['printed'] for x in entries[i + 1:i + 4]]
        if prev and nxt and e['printed'] < min(prev) and e['printed'] < min(nxt):
            e['suspect'] = '앞뒤 항목보다 쪽번호가 작음: 목차 오타일 수 있으니 본문에서 확인'
    return entries


def verify_toc_titles(idx, toc):
    """목차 쪽번호의 본문 쪽에 제목이 실제로 있는지 확인하고, 없으면 본문에서 제목 쪽을 찾아 제안."""
    recs = [r for r in load(idx) if r['kind'] == 'src']
    by_printed = {}
    for r in recs:
        if r['printed'] is not None:
            by_printed.setdefault(r['printed'], r)
    for e in toc:
        key = re.sub(r'[【】]', '', e['title_nospace'])
        page = by_printed.get(e['printed'])
        if page and key in re.sub(r'\s+', '', page['text']):
            e['check'] = 'ok'
            continue
        # 제목만 있는 표지 쪽 찾기
        cands = [r['printed'] for r in recs if r['printed'] and r['g'] > e['toc_g'] and
                 re.sub(r'\s+', '', r['text']).startswith(key)]
        e['check'] = f'목차 쪽({e["printed"]})에 제목 없음' + (f', 본문 후보 {cands[:3]}' if cands else '')
        if cands and e.get('suspect'):
            e['fixed_from'] = e['printed']
            e['printed'] = cands[0]
            e['check'] = f'목차에는 {e["fixed_from"]}쪽, 본문 확인 결과 {cands[0]}쪽으로 보정'


KV_RE = re.compile(r'(?:^|\s)([가-힣]{1,4})\s*:\s*(.+?)(?=\s+[가-힣]{1,4}\s*:|\s*\(단\s*위|$)')


def running_ranges(idx, key_filter=None, max_runs=60):
    """목차가 없을 때: 구분 쪽(제목만 있는 쪽)과 쪽 위 머리글('부서: ○○')이 바뀌는 곳으로 범위를 만든다."""
    recs = [r for r in load(idx) if r['kind'] == 'src']
    pn = lambda r: r['printed'] if r['printed'] is not None else f"g{r['g']}"
    dividers = []
    for r in recs:
        body = [l for l in r['text'].splitlines() if l.strip() and not any(p.match(l) for p in PRINTED_PATTERNS)]
        flat = re.sub(r'\s+', '', ''.join(body))
        if 0 < len(flat) <= 30 and len(body) <= 3:
            dividers.append((r, flat))
    runs = {}
    for r in recs:
        head = r['text'].splitlines()[:8]
        seen = set()
        for line in head:
            for k, v in KV_RE.findall(line):
                v = re.sub(r'\s+', ' ', v).strip()
                if k in seen or not v:
                    continue
                seen.add(k)
                lst = runs.setdefault(k, [])
                if lst and lst[-1][0] == v and lst[-1][3] == r['g'] - 1:
                    lst[-1][2], lst[-1][3] = pn(r), r['g']
                else:
                    lst.append([v, pn(r), pn(r), r['g']])
    print('목차 쪽을 찾지 못해 구분 쪽과 쪽 머리글로 범위를 만들었습니다. (인쇄 쪽번호)')
    if dividers:
        print('\n[구분 쪽] 제목만 있는 쪽. 다음 구분 쪽 앞까지를 범위로 봄')
        for i, (r, title) in enumerate(dividers):
            end = None
            if i + 1 < len(dividers):
                nxt = dividers[i + 1][0]
                end = nxt['printed'] - 1 if isinstance(nxt['printed'], int) else f"g{nxt['g'] - 1}"
            print(f'  {title:<24} {pn(r)!s:>6} ~ {end if end is not None else "끝"!s:>6}')
    for k, lst in sorted(runs.items(), key=lambda kv: len(kv[1])):
        if key_filter and k != key_filter:
            continue
        if len(lst) < 2 and not key_filter:
            continue
        print(f"\n[머리글 '{k}:'] 같은 값이 이어지는 쪽 범위 ({len(lst)}개)")
        for v, a, b, _ in lst[:None if key_filter else max_runs]:
            print(f'  {v[:40]:<40} {a!s:>6} ~ {b!s:>6}')
        if not key_filter and len(lst) > max_runs:
            print(f'  ... 외 {len(lst) - max_runs}개 (toc IDX --key {k} 로 모두 보기)')


def divider_pages(recs):
    """제목만 있는 구분 쪽 (인쇄 쪽, 붙인 제목). 국 안의 '세입', '세출' 구분 쪽과 빈 쪽은 뺀다."""
    out = []
    for r in recs:
        if r['printed'] is None:
            continue
        body = [l for l in r['text'].splitlines() if l.strip() and not any(p.match(l) for p in PRINTED_PATTERNS)]
        flat = re.sub(r'\s+', '', ''.join(body))
        if 0 < len(flat) <= 30 and len(body) <= 3 and flat not in ('세입', '세출'):
            out.append((r['printed'], flat))
    return out


def toc_ranges(idx, toc):
    """목차 항목마다 끝 쪽(end)을 정한다: 같거나 높은 단계의 다음 항목, 또는 같거나 높은 단계의 다음 구분 쪽 바로 앞.
    목차에 없는 구분 쪽(일반회계, 기타특별회계, 첨부서류 같은 큰 구분)은 가장 높은 단계로 본다."""
    recs = [r for r in load(idx) if r['kind'] == 'src']
    last = max((r['printed'] for r in recs if r['printed'] is not None), default=None)
    by_title = {}
    for e in toc:
        by_title.setdefault(re.sub(r'[【】\s]', '', e['title']), e.get('level', 1))
    bounds = [(p, by_title.get(re.sub(r'[【】]', '', t), 0)) for p, t in divider_pages(recs)]
    for i, e in enumerate(toc):
        lv = e.get('level', 1)
        cands = [x['printed'] for x in toc[i + 1:] if x.get('level', 1) <= lv and x['printed'] > e['printed']]
        cands += [p for p, blv in bounds if blv <= lv and p > e['printed']]
        e['end'] = min(cands) - 1 if cands else last
    return toc


def cmd_toc(idx, key=None):
    toc = json.load(open(os.path.join(idx, 'toc.json'), encoding='utf-8'))
    if not toc or key:
        running_ranges(idx, key)
        return
    verify_toc_titles(idx, toc)
    toc_ranges(idx, toc)
    json.dump(toc, open(os.path.join(idx, 'toc.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    for e in toc:
        end = e['end']
        flag = '' if e.get('check') == 'ok' else f'   ! {e.get("check")}'
        if e.get('suspect') and not e.get('fixed_from'):
            flag += f'   ! {e["suspect"]}'
        ind = '  ' * max(0, e.get('level', 1) - 1)
        print(f'{ind}{e["title"]:<30} {e["printed"]:>5} ~ {end if end else "끝":>5}{flag}')


def cmd_cut(idx, out, match=(), level=1, pages=(), within=()):
    """목차 범위대로 예산서를 잘라 PDF로 저장 (쪽 복사 방식이라 글꼴이 깨지지 않음).
    match: 제목에 이 글이 든 목차 항목만 (띄어쓰기 무시). 없으면 level 이하 단계 항목 전부.
    within: 이 제목 항목(예: 위원회) 범위 안의 아래 단계 항목을 하나씩 (level 이하 단계까지).
    pages: '1549-1593:계속비 사업조서' 처럼 인쇄 쪽 범위를 직접 지정."""
    from pypdf import PdfReader, PdfWriter
    meta = json.load(open(os.path.join(idx, 'meta.json'), encoding='utf-8'))
    toc = json.load(open(os.path.join(idx, 'toc.json'), encoding='utf-8'))
    if toc:
        verify_toc_titles(idx, toc)
        toc_ranges(idx, toc)
    recs = [r for r in load(idx) if r['kind'] == 'src']
    g_of = {}
    for r in recs:
        if r['printed'] is not None:
            g_of.setdefault(r['printed'], r['g'])
    off = meta.get('g_minus_printed', 0)
    gmax = max(r['g'] for r in recs)
    jobs = []
    keys = [re.sub(r'\s+', '', m) for m in match]
    flat = lambda e: re.sub(r'[【】\s]', '', e['title'])
    for e in toc:
        if (keys and any(k in flat(e) for k in keys)) or (not keys and not pages and not within and e.get('level', 1) <= level):
            jobs.append((e['title'].strip('【】 '), e['printed'], e['end']))
    for w in within:
        wk = re.sub(r'\s+', '', w)
        for par in [e for e in toc if wk in flat(e)]:
            for e in toc:
                if e is not par and par['printed'] <= e['printed'] <= (par['end'] or 10 ** 6) \
                        and par.get('level', 1) < e.get('level', 1) <= max(level, par.get('level', 1) + 1):
                    jobs.append((e['title'].strip('【】 '), e['printed'], e['end']))
    for spec in pages:
        rng, _, name = spec.partition(':')
        a, _, b = rng.partition('-')
        jobs.append((name or rng, int(a), int(b or a)))
    if not jobs:
        sys.exit('자를 범위가 없습니다. toc 명령으로 제목을 확인하고 --match 나 --pages 로 지정하세요.')
    readers = {}
    os.makedirs(out, exist_ok=True)
    lines = ['| 파일 | 인쇄 쪽 | PDF 전체 쪽 | 쪽수 |', '|---|---|---|---|']
    for title, a, b in jobs:
        if b is None:
            continue
        g0 = g_of.get(a, a + off)
        g1 = g_of.get(b, b + off)
        nxt = g_of.get(b + 1)                    # 끝 쪽 뒤의 빈 쪽(번호 없음)은 다음 범위 앞까지 붙인다
        if nxt:
            g1 = max(g1, nxt - 1)
        g1 = min(g1, gmax)
        w = PdfWriter()
        for g in range(g0, g1 + 1):
            src = next(s for s in meta['sources'] if s['g_from'] <= g <= s['g_to'])
            rd = readers.setdefault(src['file'], PdfReader(src['file']))
            w.add_page(rd.pages[g - src['g_from']])
        try:
            w.compress_identical_objects(remove_duplicates=True, remove_unreferenced=True)
        except TypeError:
            w.compress_identical_objects(remove_identicals=True, remove_orphans=True)
        if re.fullmatch(r'(\S\s+)+\S', title.strip()):      # '예 산 총 칙' 처럼 글자마다 띄운 제목은 붙인다
            title = re.sub(r'\s+', '', title)
        name = re.sub(r'[\\/:*?"<>|\s]+', '_', title).strip('_')
        fn = f'{a:04d}-{b:04d}_{name}.pdf'
        with open(os.path.join(out, fn), 'wb') as f:
            w.write(f)
        lines.append(f'| {fn} | {a}~{b} | {g0}~{g1} | {g1 - g0 + 1} |')
        print(f'{fn}: 인쇄 {a}~{b}쪽, PDF 전체 {g0}~{g1}쪽 ({g1 - g0 + 1}쪽)')
    open(os.path.join(out, '00_자른_목록.md'), 'w', encoding='utf-8').write('\n'.join(lines) + '\n')


def norm(s):
    return re.sub(r'\s+', '', s)


def cmd_search(idx, q, rng=None, aux=False, ctx=1, limit=60):
    lo, hi = (None, None)
    if rng:
        a, b = rng.split('-')
        lo, hi = int(a), int(b)
    is_num = re.fullmatch(r'△?[\d,]+(\.\d+)?', q.strip()) is not None
    qn = q.replace(',', '').replace('△', '') if is_num else norm(q)
    n = 0
    for r in load(idx):
        if (r['kind'] == 'aux') != aux:
            continue
        if lo is not None and (r['printed'] is None or not lo <= r['printed'] <= hi):
            continue
        lines = r['text'].splitlines()
        for i, l in enumerate(lines):
            hit = (qn in [x.replace(',', '') for x in re.findall(r'[\d,]+(?:\.\d+)?', l)]) if is_num else (qn in norm(l))
            if hit:
                where = f'{r["file"]} p{r["page"]}' + (f' (인쇄 {r["printed"]}쪽, 전체 {r["g"]})' if r['kind'] == 'src' else '')
                block = '\n      '.join(lines[max(0, i - ctx):i + ctx + 1])
                print(f'[{where}]\n      {block}')
                n += 1
                if n >= limit:
                    print(f'... {limit}건에서 멈춤 (--limit 로 늘리기)')
                    return


def parse_pages(spec):
    """'939-941', '833,867,901', '833,900-902' → 쪽 번호 집합."""
    out = set()
    for part in spec.replace(' ', '').split(','):
        if not part:
            continue
        a, _, b = part.partition('-')
        if not a.isdigit() or (b and not b.isdigit()):
            sys.exit(f'쪽 범위를 읽지 못함: {spec} (예: 939-941 또는 833,867,901)')
        out |= set(range(int(a), int(b or a) + 1))
    return out


def cmd_pages(idx, rng, by_pdf=False, aux=False):
    want = parse_pages(rng)
    for r in load(idx):
        if (r['kind'] == 'aux') != aux:
            continue
        key = r['g'] if by_pdf else r['printed']
        if aux and by_pdf:
            key = r['page']
        if key is not None and key in want:
            print(f'===== {r["file"]} p{r["page"]} (인쇄 {r["printed"]}, 전체 {r["g"]})')
            print(r['text'])


def main():
    try:
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('build')
    s.add_argument('idx')
    s.add_argument('files', nargs='+')
    s.add_argument('--aux', nargs='*', default=[])
    s = sub.add_parser('toc')
    s.add_argument('idx')
    s.add_argument('--key', help="목차 대신 쪽 머리글 'KEY:' 값으로 범위 보기 (예: 부서)")
    s = sub.add_parser('search')
    s.add_argument('idx')
    s.add_argument('query')
    s.add_argument('--range')
    s.add_argument('--aux', action='store_true')
    s.add_argument('--ctx', type=int, default=1)
    s.add_argument('--limit', type=int, default=60)
    s = sub.add_parser('pages')
    s.add_argument('idx')
    s.add_argument('range')
    s.add_argument('--pdf', action='store_true')
    s.add_argument('--aux', action='store_true')
    s = sub.add_parser('cut', help='목차 범위대로 PDF 자르기')
    s.add_argument('idx')
    s.add_argument('out')
    s.add_argument('--match', nargs='*', default=[], help='제목에 이 글이 든 항목만 (예: 복지보훈여성국 계속비)')
    s.add_argument('--level', type=int, default=1, help='--match 가 없을 때 이 단계 이하 항목 전부 (0 위원회, 1 국과 큰 장, 2 과)')
    s.add_argument('--pages', nargs='*', default=[], help="인쇄 쪽 범위 직접 지정 (예: '7-17:세입총괄표')")
    s.add_argument('--within', nargs='*', default=[], help='이 항목(예: 문화복지환경위원회) 안의 국, 사업소를 하나씩')
    a = ap.parse_args()
    if a.cmd == 'build':
        build(a.idx, a.files, a.aux)
    elif a.cmd == 'toc':
        cmd_toc(a.idx, a.key)
    elif a.cmd == 'search':
        cmd_search(a.idx, a.query, a.range, a.aux, a.ctx, a.limit)
    elif a.cmd == 'pages':
        cmd_pages(a.idx, a.range, a.pdf, a.aux)
    elif a.cmd == 'cut':
        cmd_cut(a.idx, a.out, a.match, a.level, a.pages, a.within)


if __name__ == '__main__':
    main()
