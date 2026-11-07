#!/usr/bin/env python3
"""한글 프로그램 없이 HWPX 쪽을 PNG로 그려 눈으로 확인 (rhwp WASM 엔진 사용).

  python3 render_check.py doc.hwpx --pages 1,5-7 --out render_dir
  python3 render_check.py doc.hwpx --find "신규사업" --out render_dir     # 그 글이 있는 쪽
  python3 render_check.py doc.hwpx --count                                 # 전체 쪽수만

처음 실행할 때 npm으로 @rhwp/core 와 sharp 를 작업 폴더 캐시에 설치한다 (1~2분).
렌더링은 한글과 줄 배치가 조금 다를 수 있다. 표가 칸 밖으로 넘치거나, 빈 표, 숫자 줄바꿈 같은
큰 문제를 찾는 용도로 쓴다.
"""
import argparse
import json
import os
import subprocess
import sys

sys.dont_write_bytecode = True     # skill 폴더에 __pycache__ 를 만들지 않음

CACHE = os.environ.get('HWPX_RENDER_CACHE', os.path.expanduser('~/.cache/hwpx-yearly-update-render'))
RHWP = os.environ.get('RHWP_VERSION', '0.8.7')

JS = r"""
import fs from 'fs';
import path from 'path';
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const rh = await import('@rhwp/core');
const wasm = fs.readFileSync(path.join(path.dirname(require.resolve('@rhwp/core')), 'rhwp_bg.wasm'));
rh.initSync({ module: wasm });
const [,, input, outDir, pagesArg, findArg] = process.argv;
const doc = new rh.HwpDocument(new Uint8Array(fs.readFileSync(input)));
const n = doc.pageCount();
let pages = [];
if (pagesArg && pagesArg !== '-') {
  for (const part of pagesArg.split(',')) {
    const [a, b] = part.split('-').map(Number);
    for (let p = a; p <= (b || a); p++) if (p >= 1 && p <= n) pages.push(p);
  }
}
const svgs = {};
if (findArg && findArg !== '-') {
  const q = findArg.replace(/\s+/g, '');
  for (let p = 1; p <= n; p++) {
    const svg = doc.renderPageSvg(p - 1);
    const text = svg.replace(/<[^>]+>/g, '').replace(/\s+/g, '');
    if (text.includes(q)) { pages.push(p); svgs[p] = svg; }
  }
}
const out = { pageCount: n, files: [] };
if (pages.length) {
  let sharp = null;
  try { sharp = (await import('sharp')).default; } catch (e) { out.note = 'sharp 없음: SVG로만 저장'; }
  fs.mkdirSync(outDir, { recursive: true });
  for (const p of [...new Set(pages)].slice(0, 30)) {
    const svg = svgs[p] || doc.renderPageSvg(p - 1);
    const base = path.join(outDir, `page_${String(p).padStart(3, '0')}`);
    if (sharp) {
      await sharp(Buffer.from(svg), { density: 110 }).flatten({ background: '#ffffff' }).png().toFile(base + '.png');
      out.files.push(base + '.png');
    } else {
      fs.writeFileSync(base + '.svg', svg);
      out.files.push(base + '.svg');
    }
  }
}
console.log(JSON.stringify(out));
"""


def ensure_tools():
    os.makedirs(CACHE, exist_ok=True)
    pj = os.path.join(CACHE, 'package.json')
    if not os.path.exists(pj):
        json.dump({'name': 'hwpx-render', 'private': True, 'type': 'module'}, open(pj, 'w'))
    if not os.path.isdir(os.path.join(CACHE, 'node_modules', '@rhwp', 'core')):
        print('렌더링 도구 설치 중 (@rhwp/core, sharp)...', file=sys.stderr)
        r = subprocess.run(['npm', 'install', '--no-audit', '--no-fund', '--silent', f'@rhwp/core@{RHWP}', 'sharp'],
                           cwd=CACHE, capture_output=True, text=True)
        if r.returncode != 0:
            # sharp 설치가 막혀도 SVG 저장은 가능
            subprocess.run(['npm', 'install', '--no-audit', '--no-fund', '--silent', f'@rhwp/core@{RHWP}'], cwd=CACHE, check=True)
    js = os.path.join(CACHE, 'render.mjs')
    with open(js, 'w', encoding='utf-8') as f:
        f.write(JS)
    return js


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('doc')
    ap.add_argument('--pages', default='-')
    ap.add_argument('--find', default='-')
    ap.add_argument('--out', default='render')
    ap.add_argument('--count', action='store_true')
    a = ap.parse_args()
    js = ensure_tools()
    pages = '-' if a.count else a.pages
    find = '-' if a.count else a.find
    r = subprocess.run(['node', js, os.path.abspath(a.doc), os.path.abspath(a.out), pages, find],
                       cwd=CACHE, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit('렌더링 실패: ' + r.stderr[-800:])
    res = json.loads(r.stdout.strip().splitlines()[-1])
    print(f'전체 {res["pageCount"]}쪽')
    for f in res['files']:
        print(f)
    if res.get('note'):
        print(res['note'])


if __name__ == '__main__':
    main()
