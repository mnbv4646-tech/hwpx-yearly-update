#!/usr/bin/env python3
"""입력 파일 점검과 PDF 손질.

  python3 check_inputs.py check 파일...            # 종류, 쪽수, 글자 깨짐, HWPX 구조
  python3 check_inputs.py compact in.pdf out.pdf   # 쪽을 그대로 복사하고 중복 글꼴을 정리해 용량 줄이기
  python3 check_inputs.py split in.pdf out_prefix 400   # 400쪽씩 쪽 복사 방식으로 나누기

글자 깨짐: 다른 프로그램으로 PDF를 자르거나 압축하면 글꼴의 글자 대응표가 깨져 '(cid:123)' 같은
코드로 읽히고, △나 % 같은 기호가 사라질 수 있다. 이런 파일은 숫자 부호를 잘못 읽을 위험이 있으므로
원본을 다시 받거나 compact/split 으로 다시 만들어야 한다.
"""
import argparse
import signal
import json
import os
import re
import sys

sys.dont_write_bytecode = True     # skill 폴더에 __pycache__ 를 만들지 않음
import zipfile


def pdf_health(path, sample=40):
    try:
        import pdfplumber
    except ImportError:
        return {'error': 'pdfplumber 없음: pip install pdfplumber --break-system-packages'}
    with pdfplumber.open(path) as pdf:
        n = len(pdf.pages)
    idx = sorted(set([0, n - 1] + [int(i * (n - 1) / max(1, sample - 1)) for i in range(sample)]))
    chars = cid = hangul = empty = 0
    with pdfplumber.open(path, pages=[i + 1 for i in idx]) as pdf:
        for pg in pdf.pages:
            t = pg.extract_text() or ''
            pg.flush_cache()
            if not t.strip():
                empty += 1
            chars += len(t)
            cid += len(re.findall(r'\(cid:\d+\)', t))
            hangul += len(re.findall(r'[가-힣]', t))
    res = {'pages': n, 'size_mb': round(os.path.getsize(path) / 1e6, 1), 'sampled': len(idx),
           'empty_pages_in_sample': empty, 'cid_per_1000_chars': round(cid * 1000 / max(1, chars), 2),
           'hangul_ratio': round(hangul / max(1, chars), 3)}
    problems = []
    if res['cid_per_1000_chars'] > 0.5:
        problems.append('글자 깨짐: 글꼴 대응표가 손상된 PDF입니다. △, %, 일부 한글이 다른 글자로 읽힙니다. '
                        '원본 PDF를 다시 받거나, 원본에서 compact/split 으로 다시 만드세요.')
    if empty > len(idx) * 0.5:
        problems.append('텍스트 없음: 스캔 이미지 PDF로 보입니다. 숫자를 글자로 읽을 수 없어 OCR이 필요합니다.')
    if res['size_mb'] > 30:
        problems.append(f'용량 {res["size_mb"]}MB: 업로드 한도를 넘을 수 있습니다. compact 로 줄여 보세요.')
    res['problems'] = problems
    return res


def hwpx_health(path):
    res = {}
    problems = []
    try:
        z = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        return {'problems': ['ZIP 형식이 아닙니다. .hwp 파일의 확장자만 바뀐 것일 수 있습니다.']}
    names = z.namelist()
    if names[0] != 'mimetype':
        problems.append('mimetype이 첫 항목이 아님')
    secs = sorted(n for n in names if re.match(r'Contents/section\d+\.xml$', n))
    res['sections'] = len(secs)
    hdr = z.read('Contents/header.xml')
    m = re.search(rb'secCnt="(\d+)"', hdr)
    if m and int(m.group(1)) != len(secs):
        problems.append(f'header.xml secCnt={m.group(1).decode()} 인데 섹션 파일은 {len(secs)}개 (한글에서 손상으로 표시됨)')
    hpf = z.read('Contents/content.hpf').decode('utf-8', 'replace')
    for s in secs:
        if s not in hpf:
            problems.append(f'content.hpf에 {s} 없음')
    if 'META-INF/container.rdf' in names:
        rdf = z.read('META-INF/container.rdf').decode('utf-8', 'replace')
        for ref in re.findall(r'Contents/section\d+\.xml', rdf):
            if ref not in names:
                problems.append(f'container.rdf가 없는 파일 {ref} 를 가리킴')
    try:
        from lxml import etree
        for s in secs:
            etree.fromstring(z.read(s))
    except Exception as e:  # noqa: BLE001
        problems.append(f'XML 오류: {e}')
    res['problems'] = problems
    return res


def check(paths, as_json=False):
    report = []
    for p in paths:
        ext = os.path.splitext(p)[1].lower()
        item = {'file': p, 'type': ext.lstrip('.')}
        if ext == '.pdf':
            item.update(pdf_health(p))
        elif ext == '.hwpx':
            item.update(hwpx_health(p))
        elif ext == '.hwp':
            item['problems'] = ['.hwp 파일은 이 skill이 다루지 않습니다. 한글에서 "다른 이름으로 저장 > HWPX"로 바꿔 다시 올려 주세요.']
        else:
            item['problems'] = []
            head = ''
            if ext in ('.md', '.txt'):
                try:
                    head = open(p, encoding='utf-8', errors='replace').read(400)
                except OSError:
                    pass
            if ext in ('.md', '.txt') and (re.search(r'규칙|rules?', os.path.basename(p), re.I) or re.search(r'#\s*(문서\s*)?규칙', head)):
                item['note'] = '규칙 파일로 보임 (색인에 넣지 말고 작업 기준으로 읽기)'
            else:
                item['note'] = '보조 자료로 보임 (source_index.py build 의 --aux 에 넣기)'
        report.append(item)
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    else:
        for it in report:
            status = '문제 있음' if it.get('problems') else '정상'
            info = {k: v for k, v in it.items() if k not in ('file', 'problems')}
            print(f'[{status}] {it["file"]}  {json.dumps(info, ensure_ascii=False)}')
            for pr in it.get('problems', []):
                print('   -', pr)
    return report


def _writer_from(path, pages=None):
    from pypdf import PdfReader, PdfWriter
    rd = PdfReader(path)
    w = PdfWriter()
    for i in (pages if pages is not None else range(len(rd.pages))):
        w.add_page(rd.pages[i])
    try:
        w.compress_identical_objects(remove_duplicates=True, remove_unreferenced=True)
    except TypeError:
        w.compress_identical_objects(remove_identicals=True, remove_orphans=True)
    return w, len(rd.pages)


def compact(src, dst):
    w, n = _writer_from(src)
    w.write(dst)
    print(f'{src} ({os.path.getsize(src) / 1e6:.1f}MB, {n}쪽) -> {dst} ({os.path.getsize(dst) / 1e6:.1f}MB)')
    same = compare_text(src, dst, list(range(0, n, max(1, n // 20))))
    print('텍스트 비교(표본):', '동일' if same else '다름 - 원본을 쓰세요')


def split(src, prefix, size):
    from pypdf import PdfReader
    n = len(PdfReader(src).pages)
    for k, s in enumerate(range(0, n, size), 1):
        e = min(n, s + size)
        w, _ = _writer_from(src, range(s, e))
        out = f'{prefix}_{k}_p{s + 1}-{e}.pdf'
        w.write(out)
        print(out, f'{os.path.getsize(out) / 1e6:.1f}MB')


def compare_text(a, b, idx):
    import pdfplumber
    with pdfplumber.open(a, pages=[i + 1 for i in idx]) as pa, pdfplumber.open(b, pages=[i + 1 for i in idx]) as pb:
        for x, y in zip(pa.pages, pb.pages):
            if (x.extract_text() or '') != (y.extract_text() or ''):
                return False
    return True


def main():
    try:
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('check')
    s.add_argument('files', nargs='+')
    s.add_argument('--json', action='store_true')
    s = sub.add_parser('compact')
    s.add_argument('src')
    s.add_argument('dst')
    s = sub.add_parser('split')
    s.add_argument('src')
    s.add_argument('prefix')
    s.add_argument('size', type=int)
    a = ap.parse_args()
    if a.cmd == 'check':
        rep = check(a.files, a.json)
        sys.exit(1 if any(it.get('problems') for it in rep if it['type'] in ('pdf', 'hwpx', 'hwp')) else 0)
    elif a.cmd == 'compact':
        compact(a.src, a.dst)
    elif a.cmd == 'split':
        split(a.src, a.prefix, a.size)


if __name__ == '__main__':
    main()
