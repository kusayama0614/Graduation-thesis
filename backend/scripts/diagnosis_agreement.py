"""誤りの種類の診断の一致率（卒業論文 5.4節(3)・6.2節）

研究者が付けた誤りの種類のラベル（前提知識ノード）と，Gemini の診断の一致率・Cohen の κ を求める。

手順（backend ディレクトリで）:
  1. ラベル付けの表を作る
       .venv_local/bin/python scripts/diagnosis_agreement.py sheet
     → docs/evaluation/diagnosis_labels.csv（Excel で開ける）
     「研究者のラベル」列には，プロトタイプの書き起こし時の誤答の分類（analyze_no1.py / analyze_trig.py）を
     ノードに対応させた仮の値を入れてある（「ラベルの根拠」列に対応の理由）。全行を確認し，必要なら直して，
     確認した行の「確認済み」列に 1 を記入する。空欄の行は一致率の計算から除く。
  2. 一致率を計算する
       .venv_local/bin/python scripts/diagnosis_agreement.py score
     → docs/evaluation/diagnosis_agreement_<日時>.md / .json
     既定では「確認済み」が 1 の行だけを使う（--include-unconfirmed で仮のラベルも含めて試算できる）。

一致率の対象は，Gemini が候補ノードから選んだ行（診断方法 llm）。候補ノードが一つしかない小問
（single_node）は選択の余地がないため別に数える。
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import sys
from collections import Counter
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_DIR = os.path.dirname(BACKEND_DIR)
sys.path.insert(0, BACKEND_DIR)

from app.learning import evaluation as ev  # noqa: E402
from app.learning import graph  # noqa: E402

DEFAULT_SHEET = os.path.join(REPO_DIR, 'docs', 'evaluation', 'diagnosis_labels.csv')
CACHE_PATH = os.path.join(REPO_DIR, 'docs', 'trial', 'diagnosis_cache.json')

# ---------------------------------------------------------------- 仮のラベル（プロトタイプの誤答の分類から）
# No.1: analyze_no1.py の ERR1（生徒ごとの誤答の分類）。分類 → (ノード, その分類が起こりうる小問)。
# ノードが None の分類は，前提知識ノードに対応しない（転記・書き方の問題）ため研究者の判断に委ねる。
NO1_CATEGORIES: Dict[str, Tuple[Optional[str], Tuple[str, ...], List[str]]] = {
    '不等号の向き（負の数で割ったときの反転）': ('inequality_manipulation', ('練習30',),
                                    ['Q031', 'Q037', 'Q063', 'Q067', 'Q068']),
    '等号の扱い（> と ≧ / = の混同）': ('inequality_manipulation', ('練習30',), ['Q005', 'Q083', 'Q104', 'Q136']),
    '近似値・範囲の誤解（16/3 を 5, 5.3 等に）': ('exact_value', ('練習30',), ['Q027', 'Q050', 'Q060', 'Q105', 'Q106']),
    '変数の取り違え（m ではなく D と記述）': (None, ('練習30',), ['Q017']),
    '−4(−m+3) の展開での符号ミス（m の値が誤り）': ('discriminant_setup', ('練習31',), ['Q027', 'Q057', 'Q113', 'Q133']),
    'm の値の転記ミス（−6 を 3 と記述）': (None, ('練習31',), ['Q066']),
    '重解 x=−b/2a の符号ミス': ('double_root', ('練習31',), ['Q016', 'Q048', 'Q103', 'Q104', 'Q118', 'Q122']),
    '重解の代入計算ミス': ('double_root', ('練習31',), ['Q095', 'Q121']),
    '重解を求めていない／片方のみ': ('double_root', ('練習31',), ['Q001', 'Q009', 'Q026', 'Q030', 'Q058', 'Q062', 'Q098']),
    '係数の読み取りミス（判別式の立式）': ('discriminant_setup', ('練習29', '練習30', '練習31'), ['Q022', 'Q034', 'Q049']),
    '実数解の個数の誤判定': ('discriminant_sign', ('練習29',), ['Q029', 'Q063', 'Q097', 'Q136', 'Q137']),
}

# No.8: analyze_trig.py の et()（小問による誤答の分類）。小問の先頭 → (分類, ノード)。
NO8_CATEGORIES = [
    ('P1_3', '対辺・隣辺の取り違え（図が回転した三角形）', 'side_identification'),
    ('P1', '辺の比の取り違え・計算（その他）', 'side_identification'),
    ('P2', '有名角の値の混同', 'special_angle_values'),
    ('P3', '三角比の表の読み違い・丸め', 'trig_table_reading'),
    ('P4', '表の逆引きの誤り', 'inverse_table_lookup'),
    ('P5', '応用（坂）：計算・丸めの誤り', 'application_modeling'),
    ('P6', '応用（塔）：立式・目の高さ・計算の誤り', 'application_modeling'),
]


def provisional_label(exercise: str, student_id: str, question: str, candidates: List[str]) -> Tuple[str, str]:
    """(仮のラベル, 根拠) を返す。決められないときはラベルを空にする。"""
    if exercise == 'No.1':
        matches = [(name, node) for name, (node, questions, students) in NO1_CATEGORIES.items()
                   if student_id in students and question in questions]
        if not matches:
            return '', 'プロトタイプの分類なし（要判断）'
        if len(matches) > 1:
            return '', '複数の分類に該当: ' + ' / '.join(name for name, _ in matches) + '（要判断）'
        name, node = matches[0]
        if node is None:
            return '', f'プロトタイプの分類「{name}」はノードに対応しない（要判断）'
        if node not in candidates:
            return '', f'プロトタイプの分類「{name}」→ {node} は候補にない（要判断）'
        return node, f'プロトタイプの分類「{name}」'
    if exercise == 'No.8':
        for prefix, name, node in NO8_CATEGORIES:
            if question == prefix or question.startswith(prefix + '_') or (len(prefix) == 2 and question.startswith(prefix)):
                if node not in candidates:
                    return '', f'プロトタイプの分類「{name}」→ {node} は候補にない（要判断）'
                return node, f'プロトタイプの分類「{name}」'
    return '', 'プロトタイプの分類なし（要判断）'


# ---------------------------------------------------------------- 表の作成

SHEET_COLUMNS = ['演習', '生徒', '小問', '判定', '初答', '正解', '候補ノード', '診断方法']
TAIL_COLUMNS = ['研究者のラベル', 'ラベルの根拠', '確認済み', 'メモ']


def latest_trial() -> Optional[str]:
    paths = [p for p in glob.glob(os.path.join(REPO_DIR, 'docs', 'trial', 'real_data_trial_*.json'))
             if not p.endswith('_fake.json')]
    return max(paths) if paths else None


def model_predictions(record: Dict[str, Any], cache: Dict[str, Any]) -> Dict[str, Dict[str, str]]:
    """診断キャッシュから，モデルごとの診断（ノードと理由）を集める。"""
    prefix = '|'.join([record['answer_sheet_id'], record['student_id'], record['question'],
                       record['answer'], record['judgment']]) + '|'
    return {key[len(prefix):]: value for key, value in cache.items() if key.startswith(prefix)}


def build_sheet(trial_path: str, cache: Dict[str, Any]) -> Tuple[List[str], List[List[str]]]:
    with open(trial_path, encoding='utf-8') as f:
        data = json.load(f)
    records = [r for rows in data['diagnoses'].values() for r in rows]
    models = sorted({m for r in records for m in model_predictions(r, cache)} | {data['meta']['model']})
    header = SHEET_COLUMNS + [f'{m} の診断' for m in models] + [f'{m} の理由' for m in models] + TAIL_COLUMNS
    rows = []
    for r in records:
        predictions = model_predictions(r, cache)
        # 試行の記録そのもの（生成に使ったモデル）の診断を優先する
        predictions[data['meta']['model']] = {'error_node': r.get('error_node'), 'diagnosis_note': r.get('diagnosis_note')}
        label, reason = provisional_label(r['answer_sheet_id'], r['student_id'], r['question'], r['nodes'])
        rows.append([r['answer_sheet_id'], r['student_id'], r['question'], r['judgment'], r['answer'], r['expected'],
                     ' '.join(r['nodes']), r.get('diagnosis_method') or '']
                    + [(predictions.get(m) or {}).get('error_node') or '' for m in models]
                    + [(predictions.get(m) or {}).get('diagnosis_note') or '' for m in models]
                    + [label, reason, '', ''])
    return header, rows


# ---------------------------------------------------------------- 一致率

def read_sheet(path: str) -> Tuple[List[str], List[Dict[str, str]]]:
    with open(path, encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        return list(reader.fieldnames or []), list(reader)


def score(rows: List[Dict[str, str]], models: List[str], include_unconfirmed: bool) -> Dict[str, Any]:
    known = set(graph.node_ids())
    usable, skipped = [], Counter()
    for row in rows:
        label = (row.get('研究者のラベル') or '').strip()
        if not label:
            skipped['ラベルが空欄'] += 1
            continue
        if not include_unconfirmed and (row.get('確認済み') or '').strip() not in ('1', '○', 'yes', 'true', 'TRUE'):
            skipped['未確認'] += 1
            continue
        if label not in known:
            skipped[f'未定義のノード（{label}）'] += 1
            continue
        usable.append(row)

    result: Dict[str, Any] = {'rows': len(rows), 'used': len(usable), 'skipped': dict(skipped), 'models': {}}
    for model in models:
        column = f'{model} の診断'
        per_method = {}
        for method in ('llm', 'single_node'):
            subset = [r for r in usable if r.get('診断方法') == method and (r.get(column) or '').strip()]
            pairs = [(r['研究者のラベル'].strip(), r[column].strip()) for r in subset]
            entry = ev.agreement(pairs)
            entry['by_exercise'] = {}
            for exercise in sorted({r['演習'] for r in subset}):
                ex_pairs = [(r['研究者のラベル'].strip(), r[column].strip()) for r in subset if r['演習'] == exercise]
                entry['by_exercise'][exercise] = {k: v for k, v in ev.agreement(ex_pairs).items() if k != 'confusion'}
            entry['disagreements'] = [
                {'exercise': r['演習'], 'student_id': r['生徒'], 'question': r['小問'], 'answer': r['初答'],
                 'label': r['研究者のラベル'].strip(), 'predicted': r[column].strip(),
                 'note': r.get(f'{model} の理由', '')}
                for r in subset if r['研究者のラベル'].strip() != r[column].strip()]
            per_method[method] = entry
        result['models'][model] = per_method
    if len(models) >= 2:  # モデル間の一致（ラベルによらない）
        a, b = models[0], models[1]
        pairs = [(r[f'{a} の診断'].strip(), r[f'{b} の診断'].strip()) for r in rows
                 if r.get('診断方法') == 'llm' and (r.get(f'{a} の診断') or '').strip() and (r.get(f'{b} の診断') or '').strip()]
        result['between_models'] = {'models': [a, b], **{k: v for k, v in ev.agreement(pairs).items() if k != 'confusion'}}
    return result


def _fmt(value: Any) -> str:
    return '—' if value is None else (f'{value:.3f}' if isinstance(value, float) else str(value))


def write_markdown(path: str, sheet_path: str, result: Dict[str, Any], include_unconfirmed: bool) -> None:
    labels = graph.node_labels()
    out = ['# 誤りの種類の診断の一致率（5.4節(3)）', '',
           f'- ラベルの表: {os.path.relpath(sheet_path, REPO_DIR)}',
           f'- 作成: {datetime.now().strftime("%Y-%m-%d %H:%M")}',
           f'- 使った行: {result["used"]} / {result["rows"]}'
           + (f'（除外: {", ".join(f"{k} {v}" for k, v in result["skipped"].items())}）' if result['skipped'] else ''),
           '- ' + ('**未確認の仮のラベルも含めた試算**' if include_unconfirmed else '「確認済み」の行だけを使用'), '']
    out += ['| モデル | 対象 | 件数 | 一致 | 一致率 | κ |', '|---|---|---|---|---|---|']
    for model, per_method in result['models'].items():
        for method, label in (('llm', 'Gemini が候補から選択'), ('single_node', '候補が1つ（参考）')):
            e = per_method[method]
            out.append(f'| {model} | {label} | {e["n"]} | {e["agree"]} | {_fmt(e["accuracy"])} | {_fmt(e["kappa"])} |')
            for exercise, x in e['by_exercise'].items():
                out.append(f'| | 　{exercise} | {x["n"]} | {x["agree"]} | {_fmt(x["accuracy"])} | {_fmt(x["kappa"])} |')
    if 'between_models' in result:
        b = result['between_models']
        out += ['', f'モデル間の一致（{b["models"][0]} と {b["models"][1]}，候補から選択した行）: '
                f'{b["agree"]}/{b["n"]}（{_fmt(b["accuracy"])}，κ={_fmt(b["kappa"])}）']
    for model, per_method in result['models'].items():
        e = per_method['llm']
        if not e['n']:
            continue
        out += ['', f'## {model}：混同行列（行: 研究者のラベル，列: 診断）', '']
        nodes = sorted(set(e['confusion']) | {p for row in e['confusion'].values() for p in row})
        out += ['| | ' + ' | '.join(nodes) + ' |', '|---|' + '---|' * len(nodes)]
        for label in nodes:
            row = e['confusion'].get(label, {})
            out.append(f'| {label} | ' + ' | '.join(str(row.get(n, '')) for n in nodes) + ' |')
        if e['disagreements']:
            out += ['', '### 一致しなかった行', '', '| 演習 | 生徒 | 小問 | 初答 | 研究者 | 診断 | 診断の理由 |',
                    '|---|---|---|---|---|---|---|']
            for d in e['disagreements']:
                out.append(f'| {d["exercise"]} | {d["student_id"]} | {d["question"]} | {d["answer"]} | '
                           f'{d["label"]} | {d["predicted"]} | {d["note"].replace("|", "／")} |')
    out += ['', '## ノード名', ''] + [f'- `{k}`: {v}' for k, v in labels.items()] + ['']
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(out))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    sub = parser.add_subparsers(dest='command', required=True)
    p_sheet = sub.add_parser('sheet', help='ラベル付けの表（CSV）を作る')
    p_sheet.add_argument('--trial', help='real_data_trial.py の全記録（JSON）。省略時は最新のもの')
    p_sheet.add_argument('--out', default=DEFAULT_SHEET)
    p_sheet.add_argument('--force', action='store_true', help='既存の表を上書きする（記入済みのラベルは失われる）')
    p_score = sub.add_parser('score', help='一致率を計算する')
    p_score.add_argument('--sheet', default=DEFAULT_SHEET)
    p_score.add_argument('--include-unconfirmed', action='store_true', help='未確認の仮のラベルも含めて試算する')
    p_score.add_argument('--out', default=os.path.join(REPO_DIR, 'docs', 'evaluation'))
    args = parser.parse_args(argv)

    if args.command == 'sheet':
        if os.path.exists(args.out) and not args.force:
            print(f'{args.out} は既にあります（記入済みのラベルを守るため上書きしません）。作り直すときは --force',
                  file=sys.stderr)
            return 1
        trial = args.trial or latest_trial()
        if not trial:
            print('試行の記録（docs/trial/real_data_trial_*.json）が見つかりません', file=sys.stderr)
            return 1
        cache = {}
        if os.path.exists(CACHE_PATH):
            with open(CACHE_PATH, encoding='utf-8') as f:
                cache = json.load(f)
        header, rows = build_sheet(trial, cache)
        os.makedirs(os.path.dirname(args.out), exist_ok=True)
        with open(args.out, 'w', encoding='utf-8-sig', newline='') as f:  # Excel で文字化けしないよう BOM 付き
            writer = csv.writer(f)
            writer.writerow(header)
            writer.writerows(rows)
        filled = sum(1 for r in rows if r[-4])
        print(f'{len(rows)} 行を書き出しました（仮のラベルあり {filled} 行，要判断 {len(rows) - filled} 行）: {args.out}')
        return 0

    if not os.path.exists(args.sheet):
        print(f'{args.sheet} がありません。先に sheet を実行してください', file=sys.stderr)
        return 1
    header, rows = read_sheet(args.sheet)
    models = [h[:-len(' の診断')] for h in header if h.endswith(' の診断')]
    result = score(rows, models, args.include_unconfirmed)
    os.makedirs(args.out, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    suffix = '_provisional' if args.include_unconfirmed else ''
    base = os.path.join(args.out, f'diagnosis_agreement_{stamp}{suffix}')
    write_markdown(f'{base}.md', args.sheet, result, args.include_unconfirmed)
    with open(f'{base}.json', 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    for model, per_method in result['models'].items():
        e = per_method['llm']
        print(f'{model}: 一致 {e["agree"]}/{e["n"]}（一致率 {_fmt(e["accuracy"])}，κ={_fmt(e["kappa"])}）')
    if not result['used']:
        print('使える行がありません。「確認済み」列に 1 を記入するか，--include-unconfirmed で試算してください')
    print(f'レポート: {base}.md')
    return 0


if __name__ == '__main__':
    sys.exit(main())
