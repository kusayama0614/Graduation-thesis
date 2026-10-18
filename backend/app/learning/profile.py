"""フェーズA: 演習の判定結果から学習者プロファイルを更新する（卒業論文 3.2節）

定着度は，前提知識ノードごとの小問の正答率 r_t を演習のたびに指数移動平均で更新する:
    c_t = α r_t + (1 − α) c_(t−1)，α = 0.3，記録がない場合の初期値 0.5
根拠となった出題数（evidence_count）を併せて記録する。

小問の結果をどのノードの観測とするか:
- 正解（書き方の問題つきを含む）: その小問が依存する全ノードに「正解」
- 誤答・不完全・書き方の問題: 診断したノード（誤りの種類）だけに「不正解」
- 空欄: 診断できないため，依存する全ノードに「不正解」
- 判読不能・判定なし: 観測としない

プロファイルは ExerciseRecord（演習ごとの記録）から決定的に再計算する。同じ演習を再適用しても
記録が置き換わるだけで，二重に更新されることはない。誤りの診断（LLM）は記録に保存するため，
再計算で LLM を呼び直すことはない。
"""
from __future__ import annotations

import json
from collections import Counter, OrderedDict
from datetime import date
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app import db
from app.learning import graph
from app.learning import judgments as J
from app.learning.llm import LLMClient
from app.models import ExerciseRecord, LearnerProfile
from app.utils.scoring import parse_answers_from_ocr, score_with_key

ALPHA = 0.3
INITIAL_CONFIDENCE = 0.5


class ExerciseError(ValueError):
    pass


# ---------------------------------------------------------------- 解答キー

def key_nodes(answer_key: Dict[str, Any]) -> Dict[str, List[str]]:
    """解答キーに付けた前提知識ノード {問番号: [node_id, ...]}。"""
    raw = answer_key.get('nodes') if isinstance(answer_key, dict) else None
    if not isinstance(raw, dict):
        return {}
    result: Dict[str, List[str]] = {}
    for question, nodes in raw.items():
        if isinstance(nodes, str):
            nodes = [n.strip() for n in nodes.replace(';', ',').split(',') if n.strip()]
        if isinstance(nodes, list):
            result[str(question).strip()] = [str(n).strip() for n in nodes if str(n).strip()]
    return result


def key_answers(answer_key: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(answer_key, dict):
        return {}
    if 'answers' in answer_key and isinstance(answer_key['answers'], dict):
        return answer_key['answers']
    return {k: v for k, v in answer_key.items() if k not in ('nodes', 'unit', 'total_questions')}


# ---------------------------------------------------------------- 小問ごとの判定

def build_exercise_items(sheet: Dict[str, Any]) -> List[Dict[str, Any]]:
    """演習プリント1枚の小問ごとの判定を作る。

    教員が確認した採点結果（question_results）があればそれを優先し，なければ解答キーと
    OCR テキストを数学的検証チェーン（score_with_key）で判定する。
    """
    answer_key = sheet.get('answer_key') or {}
    nodes_of = key_nodes(answer_key)
    answers = {str(k): v for k, v in key_answers(answer_key).items()}
    ocr_text = sheet.get('ocr_text') or ''

    auto: Dict[str, Dict[str, Any]] = {}
    if answers and ocr_text:
        scoring_key = {'answers': answers}
        for detail in score_with_key(ocr_text, scoring_key)['details']:
            auto[str(detail['q'])] = detail
    parsed = parse_answers_from_ocr(ocr_text) if ocr_text else {}

    items: List[Dict[str, Any]] = []
    confirmed = sheet.get('question_results') or []
    if confirmed:
        for result in confirmed:
            q = str(result['question'])
            judgment = result.get('judgment') or J.DEFAULT_JUDGMENT_OF_RESULT[result['result']]
            items.append({
                'question': q,
                'nodes': nodes_of.get(q, []),
                'expected': answers.get(q),
                'answer': result.get('answer') if result.get('answer') is not None else parsed.get(q),
                'corrected': bool(result.get('corrected', False)),
                'judgment': judgment,
                'judge_method': 'teacher',
            })
    elif auto:
        for q, detail in auto.items():
            items.append({
                'question': q,
                'nodes': nodes_of.get(q, []),
                'expected': detail['expected'],
                'answer': detail['actual'] or None,
                'corrected': False,
                'judgment': J.judgment_from_method(detail['method']),
                'judge_method': detail['method'],
            })
    else:
        raise ExerciseError('採点結果がありません（解答キーとOCRテキスト，または教員が確認した採点結果が必要です）')
    return items


# ---------------------------------------------------------------- 誤りの種類の診断

DIAGNOSIS_SYSTEM = (
    'あなたは高等学校数学の誤答を分析する補助者です。生徒の誤答が，候補として与えた前提知識の'
    'うちどれの誤りによるものかを一つだけ選びます。正誤はすでに数式処理で判定済みなので，'
    '判定をやり直したり，生徒の答えを直したりしないでください。'
)


def diagnose_error(item: Dict[str, Any], llm: Optional[LLMClient]) -> Tuple[Optional[str], str, str]:
    """誤答の誤りの種類（前提知識ノード）を返す: (node_id, diagnosis_method, note)。"""
    nodes = item['nodes']
    if not nodes:
        return None, 'no_node', '解答キーに前提知識ノードが付いていません'
    if len(nodes) == 1:
        return nodes[0], 'single_node', '依存するノードが一つのため，そのノードの誤りとした'
    if llm is None:
        return nodes[0], 'fallback_first_node', 'LLM が未設定のため，最初のノードの誤りとした'

    labels = graph.node_labels()
    candidates = '\n'.join(f'- {n}: {labels.get(n, n)}' for n in nodes)
    user = (
        f'小問: {item["question"]}\n正解: {item.get("expected")}\n生徒の答え（初答）: {item.get("answer")}\n'
        f'判定: {item["judgment"]}\n\n前提知識の候補:\n{candidates}\n\n'
        'この誤答の原因として最も当てはまる前提知識を一つ選び，理由を一文で述べてください。'
    )
    schema = {
        'type': 'object',
        'properties': {
            'node': {'type': 'string', 'enum': list(nodes)},
            'reason': {'type': 'string'},
        },
        'required': ['node', 'reason'],
        'additionalProperties': False,
    }
    try:
        out = llm.generate_json(system=DIAGNOSIS_SYSTEM, user=user, schema=schema,
                                schema_name='error_diagnosis', temperature=0.0)
        node = out.get('node')
        if node in nodes:
            return node, 'llm', str(out.get('reason', ''))[:500]
        note = f'LLM が候補外のノード {node!r} を返したため，最初のノードの誤りとした'
    except Exception as exc:  # LLM の失敗でプロファイル更新を止めない
        note = f'LLM の呼び出しに失敗したため，最初のノードの誤りとした（{exc.__class__.__name__}）'
    return nodes[0], 'fallback_first_node', note


# ---------------------------------------------------------------- プロファイルの計算（純粋関数）

def node_observations(record: Dict[str, Any]) -> Dict[str, int]:
    """小問1つの結果を，ノードごとの観測（1=正解, 0=不正解）に変換する。"""
    judgment = record['judgment']
    nodes = record.get('nodes') or []
    if judgment in J.EXCLUDED_JUDGMENTS or not nodes:
        return {}
    if judgment in J.CORRECT_JUDGMENTS:
        return {n: 1 for n in nodes}
    if judgment == J.BLANK:
        return {n: 0 for n in nodes}
    error_node = record.get('error_node')
    return {error_node: 0} if error_node in nodes else {n: 0 for n in nodes}


def compute_profile(records: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """演習ごとの記録から学習者プロファイルを計算する（実施日順に指数移動平均）。"""
    exercises: "OrderedDict[str, List[Dict[str, Any]]]" = OrderedDict()
    for record in sorted(records, key=lambda r: (r.get('exercise_date') or date.min, r.get('order', 0))):
        exercises.setdefault(record['answer_sheet_id'], []).append(record)

    states: Dict[str, Dict[str, Any]] = {}
    tendencies: Counter = Counter()
    judgments: Counter = Counter()
    for sheet_id, items in exercises.items():
        per_node: Dict[str, List[int]] = {}
        for item in items:
            judgments[item['judgment']] += 1
            if item['judgment'] in J.ERROR_JUDGMENTS and item.get('error_node'):
                tendencies[item['error_node']] += 1
            for node, value in node_observations(item).items():
                per_node.setdefault(node, []).append(value)
        for node, values in per_node.items():
            state = states.setdefault(node, {'confidence': INITIAL_CONFIDENCE, 'evidence_count': 0, 'history': []})
            rate = sum(values) / len(values)
            state['confidence'] = round(ALPHA * rate + (1 - ALPHA) * state['confidence'], 4)
            state['evidence_count'] += len(values)
            exercise_date = items[0].get('exercise_date')
            state['history'].append({
                'answer_sheet_id': sheet_id,
                'exercise_name': items[0].get('exercise_name'),
                'exercise_date': exercise_date.isoformat() if isinstance(exercise_date, date) else exercise_date,
                'correct_rate': round(rate, 4),
                'question_count': len(values),
                'confidence': state['confidence'],
            })
    return {
        'node_states': states,
        'error_tendencies': dict(tendencies),
        'judgment_counts': dict(judgments),
        'exercise_count': len(exercises),
    }


def confidence_of(node_states: Dict[str, Any], node_id: str) -> Tuple[float, int]:
    state = node_states.get(node_id)
    if not state:
        return INITIAL_CONFIDENCE, 0
    return float(state['confidence']), int(state['evidence_count'])


# ---------------------------------------------------------------- DB への反映

def _record_to_dict(record: ExerciseRecord, order: int) -> Dict[str, Any]:
    return {
        'answer_sheet_id': record.answer_sheet_id,
        'exercise_name': record.exercise_name,
        'exercise_date': record.exercise_date,
        'order': order,
        'question': record.question,
        'nodes': json.loads(record.nodes or '[]'),
        'judgment': record.judgment,
        'error_node': record.error_node,
    }


def rebuild_profile(teacher_db_id: str, student_id: str) -> LearnerProfile:
    rows = (ExerciseRecord.query
            .filter_by(teacher_id=teacher_db_id, student_id=student_id)
            .order_by(ExerciseRecord.created_at.asc())
            .all())
    computed = compute_profile(_record_to_dict(r, i) for i, r in enumerate(rows))
    profile = LearnerProfile.query.filter_by(teacher_id=teacher_db_id, student_id=student_id).first()
    if profile is None:
        profile = LearnerProfile(teacher_id=teacher_db_id, student_id=student_id)
        db.session.add(profile)
    profile.node_states = json.dumps(computed['node_states'], ensure_ascii=False)
    profile.error_tendencies = json.dumps(computed['error_tendencies'], ensure_ascii=False)
    profile.judgment_counts = json.dumps(computed['judgment_counts'], ensure_ascii=False)
    profile.exercise_count = computed['exercise_count']
    return profile


def apply_exercise(teacher_db_id: str, sheet: Dict[str, Any], llm: Optional[LLMClient]) -> Dict[str, Any]:
    """演習プリント1枚をプロファイルに反映する（同じプリントの記録は置き換える）。"""
    student_id = (sheet.get('student_id') or '').strip()
    if not student_id:
        raise ExerciseError('生徒ID（匿名ID）が設定されていない解答用紙は，プロファイルに反映できません')

    answer_key = sheet.get('answer_key') or {}
    unit = answer_key.get('unit') if isinstance(answer_key, dict) else None
    items = build_exercise_items(sheet)

    warnings: List[str] = []
    known = set(graph.node_ids())
    for item in items:
        unknown = [n for n in item['nodes'] if n not in known]
        if unknown:
            warnings.append(f'小問 {item["question"]}: 未知のノード {", ".join(unknown)} を無視しました')
            item['nodes'] = [n for n in item['nodes'] if n in known]
        if not item['nodes']:
            warnings.append(f'小問 {item["question"]}: 前提知識ノードが付いていないため，定着度に反映しません')

    old_rows = ExerciseRecord.query.filter_by(teacher_id=teacher_db_id, answer_sheet_id=sheet['id']).all()
    affected_students = {r.student_id for r in old_rows} | {student_id}
    for row in old_rows:
        db.session.delete(row)

    diagnoses = []
    for item in items:
        error_node, method, note = (None, None, None)
        if item['judgment'] in J.ERROR_JUDGMENTS:
            error_node, method, note = diagnose_error(item, llm)
            diagnoses.append({'question': item['question'], 'error_node': error_node,
                              'diagnosis_method': method, 'note': note})
        db.session.add(ExerciseRecord(
            teacher_id=teacher_db_id,
            student_id=student_id,
            answer_sheet_id=sheet['id'],
            exercise_name=sheet.get('test_name'),
            exercise_date=sheet.get('exam_date'),
            unit=unit,
            question=item['question'],
            nodes=json.dumps(item['nodes'], ensure_ascii=False),
            expected_answer=None if item['expected'] is None else str(item['expected']),
            initial_answer=None if item['answer'] is None else str(item['answer']),
            corrected=item['corrected'],
            judgment=item['judgment'],
            judge_method=item['judge_method'],
            error_node=error_node,
            diagnosis_method=method,
            diagnosis_note=note,
        ))
    db.session.flush()

    profiles = {sid: rebuild_profile(teacher_db_id, sid) for sid in affected_students}
    db.session.commit()
    return {
        'student_id': student_id,
        'items': items,
        'diagnoses': diagnoses,
        'warnings': warnings,
        'profile': profile_to_dict(profiles[student_id]),
    }


def profile_to_dict(profile: LearnerProfile, include_records: bool = False) -> Dict[str, Any]:
    labels = graph.node_labels()
    states = json.loads(profile.node_states or '{}')
    payload = {
        'student_id': profile.student_id,
        'exercise_count': profile.exercise_count or 0,
        'node_states': {
            node: {**state, 'label': labels.get(node, node)} for node, state in states.items()
        },
        'error_tendencies': json.loads(profile.error_tendencies or '{}'),
        'judgment_counts': json.loads(profile.judgment_counts or '{}'),
        'updated_at': profile.updated_at.isoformat() if profile.updated_at else None,
    }
    if include_records:
        rows = (ExerciseRecord.query
                .filter_by(teacher_id=profile.teacher_id, student_id=profile.student_id)
                .order_by(ExerciseRecord.exercise_date.asc(), ExerciseRecord.created_at.asc())
                .all())
        payload['records'] = [record_to_dict(r) for r in rows]
    return payload


def record_to_dict(row: ExerciseRecord) -> Dict[str, Any]:
    return {
        'answer_sheet_id': row.answer_sheet_id,
        'exercise_name': row.exercise_name,
        'exercise_date': row.exercise_date.isoformat() if row.exercise_date else None,
        'question': row.question,
        'nodes': json.loads(row.nodes or '[]'),
        'expected_answer': row.expected_answer,
        'initial_answer': row.initial_answer,
        'corrected': bool(row.corrected),
        'judgment': row.judgment,
        'judge_method': row.judge_method,
        'error_node': row.error_node,
        'diagnosis_method': row.diagnosis_method,
        'diagnosis_note': row.diagnosis_note,
    }
