"""評価の集計レポート（卒業論文 6.2節「生成した解説の数学的整合率」・6.4節「表10の比較」）

生成済みの解説の記録から，数学的整合率（5.4節(4)）と表10の指標（5.5節）を集計して，
レポート（Markdown）・全記録（JSON）・表10の表（CSV，Excel で開ける）を書き出す。LLM は呼ばない。

データの取り方は2通り:
    --trial PATH   real_data_trial.py の全記録（JSON）。省略時は docs/trial の最新のもの
    --db           システムのデータベース（画面の「③ 定期テスト問題と解説」で生成したもの）

使い方（backend ディレクトリで）:
    .venv_local/bin/python scripts/evaluation_report.py              # 最新の試行の記録から
    .venv_local/bin/python scripts/evaluation_report.py --db         # データベースから（全ての問題）

CSV の「固有の内容（研究者判定）」列は空欄で出力する。5.5節の「研究者が事前に定めた基準」で判定して記入する。
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
from typing import Any, Dict, List, Optional

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_DIR = os.path.dirname(BACKEND_DIR)
sys.path.insert(0, BACKEND_DIR)

from app.learning import evaluation as ev  # noqa: E402
from app.learning import explanation as chain  # noqa: E402

KIND_LABELS = {'master': 'マスター解説', 'individual': '積み上げ型（個別化）'}
STUDENT_KINDS = ('individual',)  # 生徒ごとの解説（引き算型は比較の対象にしないため，古い記録にあっても読まない）
OUTCOME_LABELS = {'first_ok': '初回で検算OK', 'fixed': '差し戻しで修正', 'needs_review': '教員の確認へ'}


def latest_trial(directory: str) -> Optional[str]:
    paths = [p for p in glob.glob(os.path.join(directory, 'real_data_trial_*.json')) if not p.endswith('_fake.json')]
    return max(paths) if paths else None


# ---------------------------------------------------------------- 読み込み

def problems_from_trial(path: str) -> Dict[str, Any]:
    """試行の記録を，問題ごとの {title, master, students: {sid: {individual, node_states}}} にそろえる。"""
    with open(path, encoding='utf-8') as f:
        data = json.load(f)
    problems = []
    for section in data['sections']:
        semantic = {row['student_id']: row for row in section['comparison'].get('per_student', [])}
        students = {}
        for sid, pair in section['students'].items():
            students[sid] = {
                'node_states': section['selected_profiles'][sid]['node_states'],
                'individual': pair.get('individual'),
                'semantic': {kind: (semantic.get(sid, {}).get(kind) or {}).get('semantic_sim_to_master')
                             for kind in STUDENT_KINDS},
            }
        problems.append({'title': section['problem']['title'], 'unit': section['problem']['unit'],
                         'master': section['master'], 'students': students})
    return {'source': os.path.relpath(path, REPO_DIR), 'model': data['meta'].get('model'), 'problems': problems}


def problems_from_db() -> Dict[str, Any]:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(BACKEND_DIR, '.env'))
    from app import create_app
    from app.models import ExplanationEmbedding, GeneratedExplanation, TestProblem

    app = create_app()
    problems = []
    models = Counter()
    with app.app_context():
        for problem in TestProblem.query.order_by(TestProblem.created_at).all():
            rows = (GeneratedExplanation.query.filter_by(problem_id=problem.id)
                    .order_by(GeneratedExplanation.created_at.desc()).all())
            if not rows:
                continue
            latest: Dict[Any, Any] = {}
            for row in rows:
                latest.setdefault((row.kind, row.student_id), row)
                models[row.model_name] += 1
            # 意味の類似度は，画面の比較で計算・保存した埋め込みがあればそれを使う（ここでは API を呼ばない）
            vectors: Dict[str, Dict[str, List[float]]] = {}
            for e in ExplanationEmbedding.query.filter(
                    ExplanationEmbedding.explanation_id.in_([r.id for r in latest.values()])).all():
                vectors.setdefault(e.model_name, {})[e.explanation_id] = json.loads(e.vector)
            embedding_model = max(vectors, key=lambda m: len(vectors[m])) if vectors else None
            vec = vectors.get(embedding_model, {})

            def as_trace(row):
                trace = json.loads(row.trace or '{}')
                trace.setdefault('kind', row.kind)
                trace.setdefault('status', row.status)
                trace['content'] = row.content or ''
                return trace

            master_row = latest.get(('master', None))
            students = {}
            for sid in sorted({sid for (kind, sid) in latest if sid and kind in STUDENT_KINDS}):
                pair = {kind: latest.get((kind, sid)) for kind in STUDENT_KINDS}
                snapshot_row = pair['individual']
                semantic = {}
                for kind, row in pair.items():
                    semantic[kind] = (chain.cosine_similarity(vec[row.id], vec[master_row.id])
                                      if row and master_row and row.id in vec and master_row.id in vec else None)
                students[sid] = {
                    'node_states': json.loads(snapshot_row.profile_snapshot or '{}').get('node_states', {}),
                    'individual': as_trace(pair['individual']) if pair['individual'] else None,
                    'semantic': semantic,
                }
            problems.append({'title': problem.title, 'unit': problem.unit,
                             'master': as_trace(master_row) if master_row else None, 'students': students})
    return {'source': 'database', 'model': ', '.join(m for m in models if m) or None, 'problems': problems}


# ---------------------------------------------------------------- 集計

def summarize(loaded: Dict[str, Any]) -> Dict[str, Any]:
    traces = []
    rows = []
    problem_summaries = []
    for problem in loaded['problems']:
        master = problem['master']
        master_content = master['content'] if master else None
        problem_traces = [master] if master else []
        problem_rows = []
        for sid, student in problem['students'].items():
            for kind in STUDENT_KINDS:
                trace = student.get(kind)
                if not trace:
                    continue
                problem_traces.append(trace)
                problem_rows.append({'problem': problem['title'], **ev.table10_row(
                    sid, kind, trace, trace['content'], student['node_states'], master_content,
                    student['semantic'].get(kind))})
            if master:
                problem_rows.append({'problem': problem['title'], **ev.table10_row(
                    sid, 'master', master, master_content, student['node_states'])})
        between = {}
        for kind in STUDENT_KINDS:
            contents = {sid: s[kind]['content'] for sid, s in problem['students'].items() if s.get(kind)}
            pairs = ev.pairwise_similarity(contents)
            between[kind] = {'pairs': len(pairs), 'mean_text_sim': ev.mean(p['text_sim'] for p in pairs)}
        problem_summaries.append({
            'title': problem['title'], 'students': len(problem['students']),
            'consistency': ev.consistency(problem_traces), 'between_students': between,
            'means': means_by_kind(problem_rows),
        })
        traces.extend(problem_traces)
        rows.extend(problem_rows)
    return {'consistency': ev.consistency(traces), 'problems': problem_summaries,
            'means': means_by_kind(rows), 'rows': rows}


def _round(value: Optional[float]) -> Optional[int]:
    return None if value is None else round(value)


def means_by_kind(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    result = {}
    for kind in ('master', 'individual'):
        subset = [r for r in rows if r['kind'] == kind]
        if not subset:
            continue
        result[kind] = {
            'rows': len(subset),
            'weak_steps_detailed_ratio': ev.mean(r['weak_steps_detailed_ratio'] for r in subset),
            'mastered_steps_chars': _round(ev.mean(r['mastered_steps_chars'] for r in subset if r['mastered_step_count'])),
            'total_chars': _round(ev.mean(r['total_chars'] for r in subset)),
            'with_caution': sum(1 for r in subset if r['caution_steps']),
            'text_sim_to_master': ev.mean(r['text_sim_to_master'] for r in subset),
            'semantic_sim_to_master': ev.mean(r['semantic_sim_to_master'] for r in subset),
        }
    return result


# ---------------------------------------------------------------- 出力

def _fmt(value: Any) -> str:
    if value is None:
        return '—'
    if isinstance(value, float):
        return f'{value:.3f}'
    return str(value)


def _pct(entry: Dict[str, Any]) -> str:
    return f'{entry["count"]}（{entry["rate"] * 100:.1f}%）' if entry['rate'] is not None else '—'


def write_markdown(path: str, loaded: Dict[str, Any], summary: Dict[str, Any], csv_name: str) -> None:
    c = summary['consistency']
    out = [
        '# 評価の集計（数学的整合率・表10の比較）', '',
        f'- データ: {loaded["source"]}',
        f'- 生成モデル: {loaded["model"] or "—"}',
        f'- 作成: {datetime.now().strftime("%Y-%m-%d %H:%M")}',
        '',
        '## 数学的整合率（5.4節(4)）', '',
        '| | 件数 | 初回で検算OK (a) | 差し戻しで修正 (b) | 教員の確認へ (c) |',
        '|---|---|---|---|---|',
        f'| 解説 | {c["explanation_count"]} | {_pct(c["explanations"]["first_ok"])} | '
        f'{_pct(c["explanations"]["fixed"])} | {_pct(c["explanations"]["needs_review"])} |',
    ]
    for kind, entry in c['by_kind'].items():
        out.append(f'| 　{KIND_LABELS[kind]} | {entry["count"]} | {_pct(entry["first_ok"])} | '
                   f'{_pct(entry["fixed"])} | {_pct(entry["needs_review"])} |')
    out += [
        f'| ステップ | {c["step_count"]} | {_pct(c["steps"]["first_ok"])} | {_pct(c["steps"]["fixed"])} | '
        f'{_pct(c["steps"]["needs_review"])} |', '',
        f'- 検算した計算（等式）: {c["checked_claims"]} 件，式の変形: {c["checked_relations"]} 件',
        f'- 差し戻しのきっかけになった誤り: 計算 {c["ng_claims"]} 件，式の変形 {c["ng_relations"]} 件',
        f'- 計算も変形も抜き出されず検算されなかったステップ: {c["unverified_steps"]} 件', '',
        '## 表10の指標（方式ごとの平均）', '',
        '| 方式 | 行数 | 未定着ステップを詳しく書いた割合 | 定着済みステップの文字数 | 全体の文字数 | 誤りへの注意を含む | '
        'マスターとの文字列一致度 | マスターとの意味の類似度 |',
        '|---|---|---|---|---|---|---|---|',
    ]
    for kind, m in summary['means'].items():
        out.append(f'| {KIND_LABELS[kind]} | {m["rows"]} | {_fmt(m["weak_steps_detailed_ratio"])} | '
                   f'{_fmt(m["mastered_steps_chars"])} | {_fmt(m["total_chars"])} | {m["with_caution"]} | '
                   f'{_fmt(m["text_sim_to_master"])} | {_fmt(m["semantic_sim_to_master"])} |')
    out += ['', '- マスター解説の行は，各生徒のプロファイルで同じマスター解説を評価したもの（生徒数だけ数える）。',
            '- 定着済みステップの文字数は，定着済みのステップがある行だけの平均。', '']
    for p in summary['problems']:
        pc = p['consistency']
        out += [f'## {p["title"]}（生徒 {p["students"]} 名）', '',
                f'- 整合率: 解説 {pc["explanation_count"]} 件のうち 初回OK {_pct(pc["explanations"]["first_ok"])}，'
                f'差し戻しで修正 {_pct(pc["explanations"]["fixed"])}，教員の確認へ {_pct(pc["explanations"]["needs_review"])}',
                f'- 生徒どうしの文字列一致度（平均）: 積み上げ型 {_fmt(p["between_students"]["individual"]["mean_text_sim"])}', '',
                '| 生徒 | 方式 | 検算 | 未定着ステップ（詳しく書いた割合） | 定着済みステップ（文字数） | 全体の文字数 | 注意を含むステップ | '
                '文字列一致度 | 意味の類似度 |',
                '|---|---|---|---|---|---|---|---|---|']
        for r in [r for r in summary['rows'] if r['problem'] == p['title'] and r['kind'] != 'master']:
            out.append(f'| {r["student_id"]} | {KIND_LABELS[r["kind"]]} | {OUTCOME_LABELS.get(r["outcome"], "—")} | '
                       f'{r["weak_step_count"]}（{_fmt(r["weak_steps_detailed_ratio"])}） | '
                       f'{r["mastered_step_count"]}（{r["mastered_steps_chars"]}） | {r["total_chars"]} | '
                       f'{", ".join(map(str, r["caution_steps"])) or "—"} | {_fmt(r["text_sim_to_master"])} | '
                       f'{_fmt(r["semantic_sim_to_master"])} |')
        out.append('')
    out += [f'行ごとの値は `{csv_name}` にあります（「固有の内容（研究者判定）」列に記入して使う）。', '']
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(out))


CSV_COLUMNS = [
    ('problem', '問題'), ('student_id', '生徒'), ('kind', '方式'), ('outcome', '検算'),
    ('weak_step_count', '未定着ステップ数'), ('weak_steps_detailed_ratio', '詳しく書いた割合'),
    ('mastered_step_count', '定着済みステップ数'), ('mastered_steps_chars', '定着済みステップの文字数'),
    ('total_chars', '全体の文字数'), ('caution_steps', '誤りへの注意を含むステップ'),
    ('similar_check_steps', '類題を含むステップ'),
    ('text_sim_to_master', 'マスターとの文字列一致度'), ('semantic_sim_to_master', 'マスターとの意味の類似度'),
]


def write_csv(path: str, rows: List[Dict[str, Any]]) -> None:
    with open(path, 'w', encoding='utf-8-sig', newline='') as f:  # Excel で文字化けしないよう BOM 付き
        writer = csv.writer(f)
        writer.writerow([label for _, label in CSV_COLUMNS] + ['固有の内容（研究者判定）', 'メモ'])
        for r in rows:
            values = []
            for key, _ in CSV_COLUMNS:
                value = r.get(key)
                if key == 'kind':
                    value = KIND_LABELS.get(value, value)
                elif key == 'outcome':
                    value = OUTCOME_LABELS.get(value, '')
                elif isinstance(value, list):
                    value = ' '.join(map(str, value))
                values.append('' if value is None else value)
            writer.writerow(values + ['', ''])


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--trial', help='real_data_trial.py の全記録（JSON）')
    source.add_argument('--db', action='store_true', help='システムのデータベースから集計する')
    parser.add_argument('--out', default=os.path.join(REPO_DIR, 'docs', 'evaluation'))
    args = parser.parse_args(argv)

    if args.db:
        loaded = problems_from_db()
    else:
        path = args.trial or latest_trial(os.path.join(REPO_DIR, 'docs', 'trial'))
        if not path or not os.path.exists(path):
            print('試行の記録（docs/trial/real_data_trial_*.json）が見つかりません。--trial で指定してください', file=sys.stderr)
            return 1
        loaded = problems_from_trial(path)
    if not loaded['problems']:
        print('集計できる解説がありません（先に解説を生成してください）', file=sys.stderr)
        return 1

    summary = summarize(loaded)
    os.makedirs(args.out, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    base = os.path.join(args.out, f'evaluation_{stamp}')
    write_csv(f'{base}_table10.csv', summary['rows'])
    write_markdown(f'{base}.md', loaded, summary, os.path.basename(f'{base}_table10.csv'))
    with open(f'{base}.json', 'w', encoding='utf-8') as f:
        json.dump({'source': loaded['source'], 'model': loaded['model'], **summary}, f, ensure_ascii=False, indent=1)
    c = summary['consistency']
    print(f'数学的整合率: 解説 {c["explanation_count"]} 件 — 初回OK {_pct(c["explanations"]["first_ok"])}，'
          f'差し戻しで修正 {_pct(c["explanations"]["fixed"])}，教員の確認へ {_pct(c["explanations"]["needs_review"])}')
    print(f'レポート: {base}.md')
    print(f'表10:     {base}_table10.csv')
    return 0


if __name__ == '__main__':
    sys.exit(main())
