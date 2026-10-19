import csv
import json
import os
import sys

from app.learning import evaluation as ev

SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts')
sys.path.insert(0, SCRIPTS_DIR)

import diagnosis_agreement  # noqa: E402
import evaluation_report  # noqa: E402


def _step(step_id, status='ok', attempts=1, elements=('result',), nodes=('discriminant_sign',), text='説明',
          claims=(('ok',),), relations=()):
    attempt_list = []
    for i in range(1, attempts + 1):
        last = i == attempts
        attempt_list.append({
            'attempt': i,
            'ok': last and status == 'ok',
            'claims': [{'status': c[0] if last else 'ng'} for c in claims],
            'relations': [{'status': r} for r in relations] if last else [],
        })
    return {'step_id': step_id, 'title': f'ステップ{step_id}', 'nodes': list(nodes), 'text': text,
            'decision': {'elements': list(elements)}, 'status': status, 'attempt_count': attempts,
            'attempts': attempt_list}


def _trace(kind, steps, content='本文'):
    status = 'ok' if all(s['status'] == 'ok' for s in steps) else 'needs_review'
    return {'kind': kind, 'status': status, 'steps': steps, 'content': content}


def test_consistency_counts_outcomes_and_skips_subtractive():
    traces = [
        _trace('master', [_step(1), _step(2)]),
        _trace('individual', [_step(1), _step(2, attempts=2)]),
        _trace('individual', [_step(1, status='needs_review', attempts=3)]),
        _trace('subtractive', [_step(1, attempts=0, claims=())]),
        _trace('individual', [_step(1, claims=())]),
    ]
    result = ev.consistency(traces)
    assert result['explanation_count'] == 4
    assert result['explanations']['first_ok'] == {'count': 2, 'rate': 0.5}
    assert result['explanations']['fixed']['count'] == 1
    assert result['explanations']['needs_review']['count'] == 1
    assert result['by_kind']['master']['count'] == 1
    assert result['steps']['fixed']['count'] == 1
    assert result['unverified_steps'] == 1
    # 差し戻しのきっかけになった誤り: 2回目で直ったステップの1回目（1件）と，上限に達したステップの1〜2回目（2件）
    assert result['ng_claims'] == 3


def test_agreement_and_kappa():
    pairs = [('a', 'a'), ('a', 'a'), ('b', 'b'), ('b', 'a')]
    result = ev.agreement(pairs)
    assert result['n'] == 4 and result['agree'] == 3 and result['accuracy'] == 0.75
    assert result['kappa'] == 0.5
    assert result['confusion'] == {'a': {'a': 2}, 'b': {'b': 1, 'a': 1}}
    assert ev.cohen_kappa([]) is None
    assert ev.cohen_kappa([('a', 'a'), ('a', 'a')]) == 1.0


def test_table10_row_caution_only_for_individual():
    node_states = {'discriminant_sign': {'confidence': 0.3, 'evidence_count': 1}}
    steps = [_step(1, elements=('result', 'reasoning', 'error_caution'))]
    individual = ev.table10_row('S1', 'individual', _trace('individual', steps), 'あいう', node_states, 'あいえ', 0.9)
    subtractive = ev.table10_row('S1', 'subtractive', _trace('subtractive', steps), 'あいう', node_states, 'あいえ')
    assert individual['caution_steps'] == [1] and individual['weak_steps_detailed_ratio'] == 1.0
    assert individual['outcome'] == 'first_ok' and individual['semantic_sim_to_master'] == 0.9
    assert subtractive['caution_steps'] == [] and subtractive['outcome'] is None
    assert individual['text_sim_to_master'] is not None


def test_evaluation_report_from_trial(tmp_path):
    master = _trace('master', [_step(1, elements=('result', 'reasoning'))], content='マスター解説の本文')
    individual = _trace('individual', [_step(1, elements=('result', 'reasoning', 'error_caution'))], content='個別の本文')
    subtractive = _trace('subtractive', [_step(1, attempts=0, claims=())], content='マスター解説の本文')
    trial = {
        'meta': {'model': 'fake-llm'},
        'sections': [{
            'problem': {'title': '問題1', 'unit': 'quadratic_functions'},
            'master': master,
            'students': {'S1': {'individual': individual, 'subtractive': subtractive}},
            'selected_profiles': {'S1': {'node_states': {'discriminant_sign': {'confidence': 0.3, 'evidence_count': 1}}}},
            'comparison': {'per_student': [{'student_id': 'S1', 'individual': {'semantic_sim_to_master': 0.95}}]},
        }],
        'diagnoses': {},
    }
    path = tmp_path / 'real_data_trial_20990101_000000.json'
    path.write_text(json.dumps(trial, ensure_ascii=False), encoding='utf-8')
    out = tmp_path / 'out'
    assert evaluation_report.main(['--trial', str(path), '--out', str(out)]) == 0
    md = next(out.glob('evaluation_*.md')).read_text(encoding='utf-8')
    assert '数学的整合率' in md and '(C) 積み上げ型' in md
    with open(next(out.glob('*_table10.csv')), encoding='utf-8-sig') as f:
        rows = list(csv.DictReader(f))
    assert {r['方式'] for r in rows} == {'(A) マスター解説', '(B) 引き算型', '(C) 積み上げ型'}
    c_row = next(r for r in rows if r['方式'] == '(C) 積み上げ型')
    assert c_row['誤りへの注意を含むステップ'] == '1' and c_row['マスターとの意味の類似度'] == '0.95'


def test_provisional_labels():
    label, reason = diagnosis_agreement.provisional_label(
        'No.1', 'Q031', '練習30', ['discriminant_setup', 'inequality_manipulation', 'exact_value'])
    assert label == 'inequality_manipulation' and '不等号' in reason
    # 2つの分類に該当する生徒でも，小問で区別できれば決まる
    label, _ = diagnosis_agreement.provisional_label('No.1', 'Q027', '練習31', ['discriminant_setup', 'double_root'])
    assert label == 'discriminant_setup'
    # ノードに対応しない分類（転記ミス）は空欄にして研究者の判断に委ねる
    label, reason = diagnosis_agreement.provisional_label('No.1', 'Q066', '練習31', ['discriminant_setup', 'double_root'])
    assert label == '' and '要判断' in reason
    label, _ = diagnosis_agreement.provisional_label('No.8', 'T01', 'P1_3', ['side_identification'])
    assert label == 'side_identification'
    label, _ = diagnosis_agreement.provisional_label(
        'No.8', 'T01', 'P6', ['side_identification', 'trig_table_reading', 'application_modeling'])
    assert label == 'application_modeling'


def test_diagnosis_score_uses_confirmed_rows():
    rows = [
        {'演習': 'No.1', '生徒': 'Q1', '小問': '練習30', '初答': 'm>1', '診断方法': 'llm',
         'm の診断': 'inequality_manipulation', '研究者のラベル': 'inequality_manipulation', '確認済み': '1'},
        {'演習': 'No.1', '生徒': 'Q2', '小問': '練習30', '初答': 'm<5', '診断方法': 'llm',
         'm の診断': 'discriminant_setup', '研究者のラベル': 'exact_value', '確認済み': '1'},
        {'演習': 'No.1', '生徒': 'Q3', '小問': '練習30', '初答': 'm<5', '診断方法': 'llm',
         'm の診断': 'exact_value', '研究者のラベル': 'exact_value', '確認済み': ''},
        {'演習': 'No.1', '生徒': 'Q4', '小問': '練習30', '初答': 'D<1', '診断方法': 'llm',
         'm の診断': 'exact_value', '研究者のラベル': '', '確認済み': '1'},
    ]
    result = diagnosis_agreement.score(rows, ['m'], include_unconfirmed=False)
    llm = result['models']['m']['llm']
    assert result['used'] == 2 and llm['n'] == 2 and llm['agree'] == 1
    assert result['skipped'] == {'未確認': 1, 'ラベルが空欄': 1}
    assert llm['disagreements'][0]['student_id'] == 'Q2'
    trial = diagnosis_agreement.score(rows, ['m'], include_unconfirmed=True)
    assert trial['models']['m']['llm']['n'] == 3
