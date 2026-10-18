"""実データによる解説生成の試行（卒業論文 6.4節の準備）

プロトタイプの書き起こしデータ（docs/thesis/prototype/transcription.py）を数学的検証チェーン
（verify_chain.py）で判定した結果から学習者プロファイルを作り，定期テストを想定した問題について
Chain 1〜4 で個別化した解説・マスター解説・引き算型の解説を生成して，比較の指標とともに
レポート（Markdown）と全記録（JSON）を書き出す。

使い方（backend ディレクトリで）:
    .venv_local/bin/python scripts/real_data_trial.py              # Gemini を使う（.env の GEMINI_API_KEY）
    .venv_local/bin/python scripts/real_data_trial.py --fake-llm   # LLM を呼ばずに流れだけ確認する

主なオプション:
    --problems quadratic,trig  対象の問題（既定は両方）
    --students 3               問題ごとに選ぶ生徒数（判断のパターンが異なる生徒を自動で選ぶ）
    --diagnosis llm|first      誤りの種類の診断（llm: Gemini が候補から選ぶ / first: 最初のノード）
    --no-embedding             意味の類似度（埋め込み）を計算しない
    --out ../docs/trial        出力先
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from datetime import date, datetime
from typing import Any, Dict, List, Optional

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_DIR = os.path.dirname(BACKEND_DIR)
PROTOTYPE_DIR = os.path.join(REPO_DIR, 'docs', 'thesis', 'prototype')
sys.path.insert(0, BACKEND_DIR)

from dotenv import load_dotenv  # noqa: E402

from app.learning import explanation as chain  # noqa: E402
from app.learning import graph  # noqa: E402
from app.learning import judgments as J  # noqa: E402
from app.learning.profile import compute_profile, confidence_of, diagnose_error  # noqa: E402

# ---------------------------------------------------------------- 定期テストを想定した問題

PROBLEMS = {
    'quadratic': {
        'unit': 'quadratic_functions',
        'title': '判別式と定数の範囲',
        'problem_text': '2次方程式 2x^2 + 5x + m - 3 = 0 が異なる2つの実数解をもつような定数 m の値の範囲を求めよ。',
        'correct_answer': 'm<49/8',
        'exercise': 'No.1',
    },
    'trig': {
        'unit': 'trigonometry',
        'title': '塔の高さ（仰角）',
        'problem_text': (
            '塔から水平に 20 m 離れた地点に立って塔の先端を見上げたところ，仰角は 35° であった。'
            '目の高さを 1.5 m とするとき，塔の高さを小数第2位を四捨五入して求めよ。'
            'ただし，tan35° = 0.7002 とする。'
        ),
        'correct_answer': '15.5',
        'exercise': 'No.8',
    },
}

# No.1 の各小問が依存する前提知識ノード（草稿 表3 の「主な出題・根拠」に基づく）
NO1_NODES = {
    '練習29': ['discriminant_setup', 'discriminant_sign'],
    '練習30': ['discriminant_setup', 'inequality_manipulation', 'exact_value'],
    '練習31': ['discriminant_setup', 'double_root'],
}
NO1_EXPECTED = {'練習29': '2, 0, 1', '練習30': 'm<16/3', '練習31': 'm=2 のとき x=-1，m=-6 のとき x=3'}

RESULT_TO_JUDGMENT = {
    'correct': J.CORRECT, 'correct_form_issue': J.CORRECT_FORM_ISSUE, 'wrong': J.WRONG,
    'wrong_m': J.WRONG, 'wrong_root': J.WRONG, 'incomplete': J.INCOMPLETE, 'form_issue': J.FORM_ISSUE,
    'blank': J.BLANK, 'uncertain': J.UNREADABLE,
}


# ---------------------------------------------------------------- 実データ → 演習ごとの記録

def _load_prototype():
    sys.path.insert(0, PROTOTYPE_DIR)
    import transcription  # noqa: F401
    import verify_chain
    return transcription, verify_chain


def _format_no1_answer(key: str, raw: Any) -> Optional[str]:
    if raw is None:
        return None
    if key == '練習31':
        return '，'.join(f'm={m} のとき x={x}' if x is not None else f'm={m}' for m, x in raw)
    if isinstance(raw, list):
        return ', '.join(str(v) for v in raw)
    return str(raw)


def no1_records(transcription, verify_chain) -> List[Dict[str, Any]]:
    records = []
    defaults = transcription.NO1_DEFAULT
    for row in verify_chain.run_no1():
        diffs = transcription.NO1.get(row['sid'], {})
        for key, rkey, akey, ckey in (('練習29', 'r29', 'a29', 'c29'), ('練習30', 'r30', 'a30', 'c30'),
                                      ('練習31', 'r31', 'a31', 'c31')):
            raw = diffs[akey] if akey in diffs else defaults[akey]
            records.append({
                'student_id': row['sid'], 'answer_sheet_id': 'No.1', 'exercise_name': 'No.1（判別式）',
                'exercise_date': date(2026, 9, 16), 'question': key, 'nodes': NO1_NODES[key],
                'expected': NO1_EXPECTED[key], 'answer': _format_no1_answer(key, raw),
                'judgment': RESULT_TO_JUDGMENT[row[rkey]], 'corrected': bool(row.get(ckey)),
                'source_result': row[rkey],
            })
    return records


def no8_records(transcription, verify_chain) -> List[Dict[str, Any]]:
    records = []
    truth = verify_chain.TRUTH8
    for row in verify_chain.run_no8():
        nodes = transcription.NO8_NODES[row['item']]
        answer = row.get('answer')
        records.append({
            'student_id': row['sid'], 'answer_sheet_id': 'No.8', 'exercise_name': 'No.8（三角比）',
            'exercise_date': date(2026, 10, 1), 'question': row['item'], 'nodes': nodes,
            'expected': ', '.join(str(v) for v in truth[row['item']]),
            'answer': None if answer is None else ', '.join('空欄' if a is None else str(a) for a in answer),
            'judgment': RESULT_TO_JUDGMENT[row['result']], 'corrected': bool(row.get('corrected')),
            'source_result': row['result'], 'note': row.get('note'),
        })
    return records


def diagnose_all(records: List[Dict[str, Any]], llm, mode: str, log) -> None:
    targets = [r for r in records if r['judgment'] in J.ERROR_JUDGMENTS]
    for index, record in enumerate(targets, start=1):
        if mode == 'llm':
            node, method, note = diagnose_error(record, llm)
        else:
            node, method, note = diagnose_error(record, None)
        record.update(error_node=node, diagnosis_method=method, diagnosis_note=note)
        if method == 'llm' and index % 10 == 0:
            log(f'  診断 {index}/{len(targets)}')


def build_profiles(records: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    by_student: Dict[str, List[Dict[str, Any]]] = {}
    for order, record in enumerate(records):
        by_student.setdefault(record['student_id'], []).append({**record, 'order': order})
    return {sid: compute_profile(rows) for sid, rows in by_student.items()}


# ---------------------------------------------------------------- 生徒の選び方

def select_students(steps, profiles: Dict[str, Dict[str, Any]], count: int) -> List[str]:
    """判断のパターン（どのステップに何を含めるか）が異なる生徒を選ぶ。詳しく書くステップが多い順。"""
    patterns: Dict[tuple, List[str]] = {}
    for sid in sorted(profiles):
        plan = chain.plan_explanation(steps, profiles[sid]['node_states'], profiles[sid]['error_tendencies'])
        signature = tuple(tuple(d['elements']) for d in plan)
        patterns.setdefault(signature, []).append(sid)

    def weight(signature):
        return sum(len(elements) for elements in signature)

    ordered = sorted(patterns.items(), key=lambda kv: (-weight(kv[0]), kv[1][0]))
    chosen = [sids[0] for _, sids in ordered[:max(count - 1, 1)]]
    concise = min(patterns.items(), key=lambda kv: weight(kv[0]))[1][0]  # 全ステップ簡潔な生徒を必ず含める
    if concise not in chosen:
        chosen.append(concise)
    return chosen[:count] if len(chosen) > count else chosen


# ---------------------------------------------------------------- 比較

def comparison(results: Dict[str, Any], node_states_of: Dict[str, Dict[str, Any]], embedder) -> Dict[str, Any]:
    master = results['master']
    rows = []
    texts = {'master': master['content']}
    for sid, pair in results['students'].items():
        texts[f'individual:{sid}'] = pair['individual']['content']
        texts[f'subtractive:{sid}'] = pair['subtractive']['content']
    vectors = {}
    semantic_error = None
    if embedder is not None:
        try:
            keys = list(texts)
            for key, vector in zip(keys, embedder.embed([texts[k] for k in keys])):
                vectors[key] = vector
        except Exception as exc:  # 埋め込みに失敗しても文字列の類似度は出す
            semantic_error = f'{exc.__class__.__name__}: {exc}'

    def sem(a, b):
        return chain.cosine_similarity(vectors[a], vectors[b]) if a in vectors and b in vectors else None

    for sid, pair in results['students'].items():
        node_states = node_states_of[sid]
        rows.append({
            'student_id': sid,
            'individual': {**chain.explanation_metrics(pair['individual'], node_states),
                           'text_sim_to_master': chain.text_similarity(pair['individual']['content'], master['content']),
                           'semantic_sim_to_master': sem(f'individual:{sid}', 'master')},
            'subtractive': {**chain.explanation_metrics(pair['subtractive'], node_states),
                            'text_sim_to_master': chain.text_similarity(pair['subtractive']['content'], master['content']),
                            'semantic_sim_to_master': sem(f'subtractive:{sid}', 'master')},
            'master': chain.explanation_metrics(master, node_states),
        })
    between = {}
    sids = list(results['students'])
    for kind in ('individual', 'subtractive'):
        pairs = [(a, b) for i, a in enumerate(sids) for b in sids[i + 1:]]
        text = [chain.text_similarity(results['students'][a][kind]['content'], results['students'][b][kind]['content'])
                for a, b in pairs]
        semantic = [s for s in (sem(f'{kind}:{a}', f'{kind}:{b}') for a, b in pairs) if s is not None]
        between[kind] = {
            'pairs': len(pairs),
            'mean_text_sim': round(sum(text) / len(text), 3) if text else None,
            'mean_semantic_sim': round(sum(semantic) / len(semantic), 3) if semantic else None,
        }
    return {'per_student': rows, 'between_students': between, 'semantic_error': semantic_error,
            'embedding_model': getattr(embedder, 'model_name', None) if vectors else None}


# ---------------------------------------------------------------- レポート

def _verification_summary(trace: Dict[str, Any]) -> str:
    lines = []
    for step in trace['steps']:
        last = step['attempts'][-1] if step['attempts'] else {}
        counts = last.get('counts') or {}
        final = last.get('final_check')
        final_text = ''
        if final:
            final_text = f'，結論の照合: {"一致" if final.get("is_correct") else ("不一致" if final.get("is_correct") is False else "抜き出せず")}'
        lines.append(f'| {step["step_id"]} | {step["title"]} | {step["status"]} | {step["attempt_count"]} | '
                     f'{counts.get("ok", 0)} / {counts.get("ng", 0)} / {counts.get("definition", 0)} / '
                     f'{counts.get("unverifiable", 0)}{final_text} |')
    return ('| ステップ | 内容 | 状態 | 生成回数 | 検算 OK / NG / 定義 / 検算不能 |\n|---|---|---|---|---|\n'
            + '\n'.join(lines))


def _plan_table(trace: Dict[str, Any]) -> str:
    lines = []
    for step in trace['steps']:
        decision = step['decision']
        node_text = '，'.join(f'{n}（{decision["node_confidences"].get(n, {}).get("confidence", "-")}）'
                             for n in step['nodes']) or '—'
        elements = '，'.join(chain.ELEMENT_LABELS[e] for e in decision['elements'])
        lines.append(f'| {step["step_id"]} | {step["title"]} | {node_text} | {elements} |')
        for reason in decision['reasons']:
            lines.append(f'| | | | ↳ {reason} |')
    return '| ステップ | 内容 | 依存ノード（定着度） | 含めた要素 |\n|---|---|---|---|\n' + '\n'.join(lines)


def _demote(markdown: str) -> str:
    """生成した解説の見出しを，レポートの見出しより下のレベルにする。"""
    return '\n'.join('###' + line if line.startswith('#') else line for line in markdown.splitlines())


def write_report(path: str, meta: Dict[str, Any], sections: List[Dict[str, Any]]) -> None:
    out = [f'# 実データによる解説生成の試行（{meta["started_at"]}）', '']
    out += [
        f'- LLM: `{meta["model"]}`（thinking level: {meta["thinking_level"] or "モデルの既定"}）',
        f'- 埋め込み: `{meta["embedding_model"] or "未計算"}`',
        f'- 誤りの診断: {meta["diagnosis"]}',
        f'- 所要時間: {meta["elapsed_sec"]} 秒',
        '- データ: docs/thesis/prototype の書き起こし（匿名ID）を verify_chain.py で判定した結果',
        '- 生徒IDは単元ごとに別（No.1=Q###, No.8=T##）で，1人あたり演習1回分のため定着度は 0.35〜0.65 に収まる'
        '（定義の復習・類題の要素は原理的に発動しない）。',
        '',
    ]
    for section in sections:
        problem = section['problem']
        out += [f'## {problem["title"]}（{graph.get_unit(problem["unit"])["label"]}）', '',
                f'> {problem["problem_text"]}', '', f'正しい答え: `{problem["correct_answer"]}`', '']
        out += ['### プロファイルの概要（{0}，{1}人）'.format(problem['exercise'], section['profile_summary']['students']), '',
                '| ノード | 平均定着度 | 0.5未満の生徒 | 誤りの記録がある生徒 |', '|---|---|---|---|']
        for node, row in section['profile_summary']['nodes'].items():
            out.append(f'| {node}（{graph.node_labels()[node]}） | {row["mean"]} | {row["below"]} | {row["with_errors"]} |')
        diag = section['diagnosis_summary']
        out += ['', f'誤りの診断: {diag["total"]} 件（方法別: {diag["by_method"]}，ノード別: {diag["by_node"]}）', '']
        out += ['### Chain 1: 解答ステップ', '']
        for step in section['steps']:
            out.append(f'{step["id"]}. **{step["title"]}** — {step["description"]}（結果の目安: {step["result"]}；ノード: {", ".join(step["nodes"]) or "なし"}）')
        out += ['', '### マスター解説 (A)', '', _verification_summary(section['master']), '', _demote(section['master']['content']), '']
        for sid, pair in section['students'].items():
            individual = pair['individual']
            out += [f'### 生徒 {sid}', '', '**Chain 2 の判断**', '', _plan_table(individual), '',
                    '**検算（Chain 4）**', '', _verification_summary(individual), '']
            if individual['warnings']:
                out += ['警告: ' + ' / '.join(individual['warnings']), '']
            out += ['<details><summary>積み上げ型の解説 (C)</summary>', '', _demote(individual['content']), '', '</details>', '',
                    '<details><summary>引き算型の解説 (B)</summary>', '', _demote(pair['subtractive']['content']), '', '</details>', '']
        comp = section['comparison']
        out += ['### 比較（表10）', '', '| 生徒 | 方式 | 未定着ステップのうち詳しく書いた割合 | 定着済みステップの文字数 | 全体の文字数 | マスターとの文字列類似度 | マスターとの意味の類似度 |',
                '|---|---|---|---|---|---|---|']
        for row in comp['per_student']:
            for kind, label in (('individual', '積み上げ型 (C)'), ('subtractive', '引き算型 (B)'), ('master', 'マスター (A)')):
                m = row[kind]
                out.append(f'| {row["student_id"]} | {label} | {m["weak_steps_detailed_ratio"]} | {m["mastered_steps_chars"]} | '
                           f'{m["total_chars"]} | {m.get("text_sim_to_master", "—")} | {m.get("semantic_sim_to_master", "—")} |')
        out += ['', '生徒間の類似度: ' + '，'.join(
            f'{k}: 文字列 {v["mean_text_sim"]} / 意味 {v["mean_semantic_sim"]}（{v["pairs"]}組）'
            for k, v in comp['between_students'].items())]
        if comp['semantic_error']:
            out.append(f'（埋め込みの計算に失敗: {comp["semantic_error"]}）')
        out.append('')
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(out))


# ---------------------------------------------------------------- テスト用の LLM

class _FakeLLM:
    model_name = 'fake-llm'

    def generate_json(self, *, system, user, schema, schema_name, temperature=0.0):
        if schema_name == 'error_diagnosis':
            return {'node': schema['properties']['node']['enum'][0], 'reason': '（fake）'}
        if schema_name == 'solution_steps':
            nodes = schema['properties']['steps']['items']['properties']['nodes']['items']['enum']
            return {'steps': [{'title': f'ステップ{i}', 'description': '（fake）', 'result': '', 'nodes': [n]}
                              for i, n in enumerate(nodes[:3], start=1)]}
        return {'claims': [{'lhs': '1+1', 'rhs': '2', 'lhs_text': '1+1', 'rhs_text': '2'}], 'final_answer': ''}

    def generate_text(self, *, system, user, temperature=0.3):
        return '（fake）1+1 = 2 です。'


class _FakeEmbedding:
    model_name = 'fake-embedding'

    def embed(self, texts):
        return [[float(len(t)), 1.0] for t in texts]


# ---------------------------------------------------------------- 本体

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--problems', default='quadratic,trig')
    parser.add_argument('--students', type=int, default=3)
    parser.add_argument('--diagnosis', choices=('llm', 'first'), default='llm')
    parser.add_argument('--no-embedding', action='store_true')
    parser.add_argument('--fake-llm', action='store_true')
    parser.add_argument('--out', default=os.path.join(REPO_DIR, 'docs', 'trial'))
    args = parser.parse_args(argv)

    load_dotenv(os.path.join(BACKEND_DIR, '.env'))
    started = time.time()
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')

    def log(message):
        print(f'[{time.time() - started:6.0f}s] {message}', flush=True)

    if args.fake_llm:
        llm, embedder = _FakeLLM(), (None if args.no_embedding else _FakeEmbedding())
    else:
        from app.learning.llm import GeminiChatClient, GeminiEmbeddingClient
        from app.utils.gemini import gemini_api_key
        key = gemini_api_key()
        if not key:
            print('GEMINI_API_KEY が backend/.env に設定されていません', file=sys.stderr)
            return 1
        llm = GeminiChatClient(key)
        embedder = None if args.no_embedding else GeminiEmbeddingClient(key)

    transcription, verify_chain = _load_prototype()
    record_sets = {'No.1': no1_records(transcription, verify_chain), 'No.8': no8_records(transcription, verify_chain)}

    sections = []
    all_records = {}
    for name in [p.strip() for p in args.problems.split(',') if p.strip()]:
        problem = dict(PROBLEMS[name])
        records = record_sets[problem['exercise']]
        log(f'{problem["exercise"]}: 誤りの種類を診断（{args.diagnosis}）')
        diagnose_all(records, llm, args.diagnosis, log)
        profiles = build_profiles(records)
        all_records[problem['exercise']] = records

        unit_nodes = graph.node_ids(problem['unit'])
        node_rows = {}
        for node in unit_nodes:
            values = [confidence_of(p['node_states'], node)[0] for p in profiles.values() if node in p['node_states']]
            if values:
                node_rows[node] = {
                    'mean': round(sum(values) / len(values), 3),
                    'below': sum(1 for v in values if v < chain.DETAIL_THRESHOLD),
                    'with_errors': sum(1 for p in profiles.values() if p['error_tendencies'].get(node)),
                }
        errors = [r for r in records if r.get('diagnosis_method')]

        log(f'{problem["title"]}: Chain 1（解答ステップへの分解）')
        problem['steps'] = chain.decompose_problem(llm, problem['unit'], problem['problem_text'])
        students = select_students(problem['steps'], profiles, args.students)
        log(f'  選んだ生徒: {", ".join(students)}')

        log('  マスター解説 (A) を生成')
        master = chain.generate_explanation(llm, problem, kind='master')
        results = {'master': master, 'students': {}}
        for sid in students:
            log(f'  生徒 {sid}: 積み上げ型 (C) を生成')
            p = profiles[sid]
            individual = chain.generate_explanation(llm, problem, p['node_states'], p['error_tendencies'])
            subtractive = chain.build_subtractive(problem, master, p['node_states'], p['error_tendencies'])
            results['students'][sid] = {'individual': individual, 'subtractive': subtractive}

        log('  比較の指標を計算')
        comp = comparison(results, {sid: profiles[sid]['node_states'] for sid in students}, embedder)
        sections.append({
            'problem': problem, 'steps': problem['steps'], 'master': master, 'students': results['students'],
            'comparison': comp,
            'profile_summary': {'students': len(profiles), 'nodes': node_rows},
            'diagnosis_summary': {'total': len(errors),
                                  'by_method': dict(Counter(r['diagnosis_method'] for r in errors)),
                                  'by_node': dict(Counter(r['error_node'] for r in errors))},
            'selected_profiles': {sid: profiles[sid] for sid in students},
        })

    os.makedirs(args.out, exist_ok=True)
    suffix = '_fake' if args.fake_llm else ''
    md_path = os.path.join(args.out, f'real_data_trial_{stamp}{suffix}.md')
    json_path = os.path.join(args.out, f'real_data_trial_{stamp}{suffix}.json')
    meta = {
        'started_at': datetime.fromtimestamp(started).strftime('%Y-%m-%d %H:%M'),
        'model': getattr(llm, 'model_name', None),
        'thinking_level': os.getenv('GEMINI_THINKING_LEVEL', '').strip() or None,
        'embedding_model': next((s['comparison']['embedding_model'] for s in sections
                                 if s['comparison']['embedding_model']), None),
        'diagnosis': ('最初のノード' if args.diagnosis == 'first'
                      else 'テスト用の LLM（fake）' if args.fake_llm else 'Gemini が候補ノードから選択'),
        'elapsed_sec': round(time.time() - started),
    }
    write_report(md_path, meta, sections)
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump({'meta': meta, 'sections': sections,
                   'diagnoses': {k: [r for r in v if r.get('diagnosis_method')] for k, v in all_records.items()}},
                  f, ensure_ascii=False, indent=1, default=str)
    log(f'レポート: {md_path}')
    log(f'全記録:   {json_path}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
