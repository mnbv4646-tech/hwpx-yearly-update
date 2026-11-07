# 편집 계획(plan.json) 명세

`python3 hwpx_ops.py apply 입력.hwpx plan.json -o 출력.hwpx [--title "문서 속성 제목"]`

```json
{
  "defaults": {"rate_mode": "round", "rate_digits": 2, "zero": "-"},
  "ops": [ {"op": "...", ...}, ... ]
}
```

- 단락(`s0:p12`)과 표(`t7`) 주소는 `hwpx_ops.py dump`, `table`, `find` 로 확인한 **적용 전 문서** 기준이다. 계획 안의 단락과 표 주소를 먼저 해석한 뒤 차례로 적용하므로, 앞 연산이 단락이나 표를 넣고 지워도 뒤 연산의 주소는 그대로 맞는다.
- 표 안의 **행과 열 번호**(`set_cell`, `set_cells`, `set_compare_row` 의 row, col)는 그 연산을 적용하는 때의 표 기준이다. 같은 계획에서 `fill_rows` 로 행을 바꾼 표의 칸을 고치려면 바뀐 뒤의 행 번호를 쓴다 (보통 `fill_rows` 의 rows 에 값을 다 넣는 편이 쉽다).
- 단락 `s0:p12`, 표 `t7`, 칸은 `table` + `row` + `col` (병합 칸은 시작 위치).
- `copy_table`, `insert_para` 의 `as` 로 붙인 별칭(`@new1`)은 같은 계획의 뒤 연산에서 주소처럼 쓴다.
- 칸 글자에서 `\n` 은 칸 안 줄바꿈(단락)이다. 줄마다 앞 기호(∘, -, ▷, ※, 1.)로 역할을 보고, 원래 칸에서 같은 역할의 줄 서식을 쓴다. `∘ 편성사유` 같은 제목 줄은 같은 제목 줄의 서식을, 그 아래 `  - ...` 줄은 그 제목 아래 `-` 줄의 서식을 쓴다. 그래서 줄 수가 작년과 달라도 제목은 굵게, 항목은 보통 글씨로 맞는다.
- 줄 구성이 작년과 같으면(줄 수와 기호가 같으면) 제자리에서 글자만 바꾼다. 내용을 거의 새로 쓴 줄은 공통 앞부분(`  - 내    용 : `) 뒤를 그 역할의 주된 글자 모양 한 가지로 맞춘다.
- 글자를 바꿀 때는 바뀐 부분만 고치므로 굵은 글씨 같은 글자 모양이 유지된다.
- 수식 칸(=SUM 등)은 글자와 함께 수식의 마지막 결과값도 바뀐다. 복제한 행의 수식은 지워진다.

## 연산

| op | 필수 | 선택 | 하는 일 |
|---|---|---|---|
| `set_cell` | table, row, col, text | | 칸 글자 바꾸기 |
| `set_cells` | table, cells: [[row, col, text], ...] | | 여러 칸 한꺼번에 |
| `set_para` | para, text | | 단락 글자 바꾸기. `**…**`로 감싼 부분은 굵게 (작년 같은 단락의 글자 모양을 굵게만 바꿔 씀). 줄 앞 빈칸은 원래 단락 것을 이어 받음 |
| `append_text` | para, text | | 단락 끝에 덧붙이기 (예: " : 해당없음") |
| `replace` | find, replace | scope, regex | 범위 안 모든 단락에서 글자 바꾸기 |
| `year_shift` | scope | delta(기본 1), only("headers" 기본, "all") | 연도 바꾸기. headers는 제목 상자, 표 머리 행, 짧은 단락만. 2025→2026, ’25.→’26. |
| `set_compare_row` | table, row, cur, prev | page, extra{열: 글자}, cols{cur,prev,diff,rate,page}, rate_mode, rate_digits | 비교표 한 행: 올해, 전년을 넣고 증감액, 증감률을 계산해 넣음. 재원 줄("시 1,000")은 같은 이름끼리 증감 계산 |
| `rewrite_sentence` | para, cur, prev | unit(기본 천원), digits, rate_mode, text_if_zero | "~원으로 ~보다 X% 증가한 ~원이 증액되었음" 꼴 문장의 금액, 비율, 증가/감소, 증액/감액을 바꿈. 원래 띄어쓰기와 표기 방식(62백만원 등)을 따름 |
| `fill_rows` | table, data_start, rows | data_end, template_row, merge_cols, group_size, group_merge_cols | 데이터 행을 지우고 rows 만큼 다시 만듦 (아래). 작년 금액 칸에 재원 줄이 있었으면 새 행 값도 `"1,450\n시 1,450"`처럼 재원 줄을 붙여 준다 |
| `delete_rows` | table, start, end | | [start, end) 행 지우기 |
| `delete_table` | table | | 표 지우기 (표만 있는 단락이면 단락째) |
| `copy_table` | source, after | as | 표를 복사해 after 단락 뒤에 넣기 |
| `insert_para` | after, like, text | as | like 단락의 서식으로 새 단락 넣기. 같은 after 로 여러 번 넣으면 계획 순서대로 이어진다. `**…**`는 굵게, 줄 앞 빈칸은 like 단락 것을 따름 |
| `shift_cols` | table, map{원래 열: 옮길 열} | data_start(기본 1), clear[열] | 연도별 표에서 이전 연도 값을 오른쪽 열로 한꺼번에 옮김 (값은 옮기기 전 기준). 예: 2025~2022 열 → 2026 자리를 비우고 한 칸씩 오른쪽 |
| `page_break` | para | on(기본 true) | 단락 앞에서 쪽 나누기 (쪽 맨 아래 홀로 남은 제목을 다음 쪽으로) |
| `delete_para` | para | | 단락 지우기 |

### fill_rows

```json
{"op": "fill_rows", "table": "t43", "data_start": 2,
 "rows": [["기존산발효조 준설", "43,750\n시   43,750", "∘ 사업개요\n  - 내    용 : ...", "946"],
          ["환경공무직 퇴직금", "248,092\n시  248,092", "∘ 사업개요\n  - 대    상 : ...", "950"]]}
```
- `data_start` 부터 `data_end`(기본: 표 끝) 앞까지를 비우고, `template_row`(기본 data_start)를 복제해 채운다. 합계 행이 데이터 앞에 있으면 data_start 를 그 다음 행으로 잡고, 합계는 `set_cell` 이나 `set_compare_row` 로 따로 쓴다. 합계 행이 표 끝에 있으면 `data_end: -1`.
- 각 행 값의 개수 = 그 행의 칸 수 (병합을 푼 뒤 기준, 즉 `table` 명령으로 본 머리 행의 칸 수). `{"0": "...", "2": "..."}` 처럼 열 번호로 줄 수도 있다.
- `merge_cols: [0]`: 0열에서 위아래로 같은 글자가 이어지면 하나로 병합 (부서명 열 등). 각 행에 부서명을 모두 적어 준다.
- 묶음 행 (계속비 표의 기존/변경/증감 3행): `"group_size": 3, "group_merge_cols": [0, 1, 8]`, rows 의 항목 하나가 `[[1행], [2행], [3행]]`.
- 위에서 내려오는 병합 칸이 범위 안까지 걸치면 오류가 난다. data_start 를 그 병합 칸이 시작하는 행으로 옮긴다.

### 칸 너비와 줄바꿈

칸 너비는 바꾸지 않는다. 좁은 칸(사업명, 증감액)에 긴 글을 넣으면 여러 줄로 넘어가거나 고정 높이 행에서 잘린다.
- 사업명은 짧게 쓰고, 편성목 이름 같은 덧붙임은 개요 칸에 `  - 편 성 목 : 401 시설비및부대비` 처럼 옮긴다.
- 다른 블록 표를 `copy_table`로 가져올 때는 열 너비가 비슷한 표를 고른다 (같은 블록의 비슷한 표가 가장 좋다).
- 행 수를 바꾼 표는 `render_check.py`로 보고, 잘리거나 넘친 칸이 있으면 글을 줄인다.

### set_compare_row

```json
{"op": "set_compare_row", "table": "t41", "row": 3, "cur": "1,608,560", "prev": "1,704,010", "page": "941",
 "extra": {"5": "∘ 음식물류폐기물 반입수수료 단가 상승"}}
```
- 열 역할은 머리 행에서 찾는다 (연도가 가장 큰 열 = 올해, 다음 = 전년, '증감', '률', '페이지'). 못 찾으면 `"cols": {"cur": 1, "prev": 2, "diff": 3, "rate": 4, "page": 6}`.
- 전년이 0이면 증감률은 `defaults.zero` (기본 "-"). 규칙 파일에 다른 표기(예: "순증")가 있으면 그것을 쓴다.
- 증감률 자리수는 `rate_digits`, 버림은 `"rate_mode": "trunc"`. 작년 칸에 `%`가 붙어 있었으면 붙여 쓴다.

### rewrite_sentence

```json
{"op": "rewrite_sentence", "para": "s0:p151", "cur": 1860185, "prev": 2095627}
```
- 문장 안 첫 금액 = 올해, 마지막 금액 = 증감액, 첫 % = 증감률로 본다. 금액이 하나뿐인 문장도 된다.
- 증감이 0이면 오류가 난다. 규칙 파일이나 작년 문서의 "변동없음" 문장을 보고 `set_para` 로 쓴다.
- 문장 구조 자체가 다르면 `korean_amount.py fmt`, `rate` 로 값을 만들어 `set_para` 로 쓴다.

## 계획 밖에서 쓰는 도구

```bash
python3 korean_amount.py fmt 993160 --unit 천원                      # 9억 9천3백만원
python3 korean_amount.py fmt 62000 --unit 천원 --like "45백만원"      # 62백만원
python3 korean_amount.py rate 653665 6577257                         # 9.94
echo '[{"text":"...","cur":1860185,"prev":2095627}]' | python3 korean_amount.py batch
```

## 라이브러리로 직접 고칠 때

```python
import sys; sys.path.insert(0, SK)
import hwpx_ops as H
doc = H.HwpxDoc('work/step_04.hwpx')
p = doc.para('s0:p210'); H.set_text(p, '새 글')
tc = doc.cell('t51', 3, 2); H.set_cell_text(tc, '1,000\n시 1,000')
doc.save('work/step_05.hwpx')
```
`doc.save` 가 섹션 수, 패키지 목록, 미리보기 글을 함께 맞춘다. 다른 방법으로 zip을 다시 묶지 않는다.
