"""フェーズB: 積み上げ型の解説生成（卒業論文 3.4節・3.5節，表5）

Chain 1  問題文を誰にとっても省略できない解答ステップに分解し，各ステップが依存する前提知識ノードを
         付ける（LLM。出力は JSON Schema でノード名の選択肢に制約）。1問につき1回だけ実行する。
Chain 2  ステップごとに，生徒の定着度から「含める要素」をプログラムの条件分岐で決め（LLM は呼ばない），
         その方針で LLM が説明を一から書く。
Chain 3  各ステップの説明を順に結合する（プログラム）。
Chain 4  説明中の「左辺 = 右辺」の計算を LLM が式として抜き出し，sympy で検算する。誤りがあれば
         どの式が誤りで正しい値は何かを添えて Chain 2 に差し戻す。上限を超えたら教員の確認に回す。

詳しさの決定（3.4節の方式(1)）: ステップが依存するノードのうち最も定着度の低いものの値 c に応じて，
含める要素を切り替える。閾値は ELEMENT_RULES に集約しており，判断の理由は全て trace に残る。
"""
from __future__ import annotations

import difflib
import math
from typing import Any, Dict, List, Optional, Tuple

from app.learning import graph
from app.learning.llm import LLMClient
from app.learning.profile import confidence_of
from app.utils.math_verification import check_claim, judge_answer

# ---------------------------------------------------------------- 詳しさの決定規則（Chain 2）

DETAIL_THRESHOLD = 0.5            # 草稿 3.4節の閾値: これを下回るノードがあれば「詳しく書く」
DEFINITION_REVIEW_THRESHOLD = 0.35
SIMILAR_CHECK_THRESHOLD = 0.2
CAUTION_CONFIDENCE_LIMIT = 0.7    # 誤りの記録があっても，定着度がこれ以上なら注意は添えない
MAX_ATTEMPTS = 3                  # 1ステップあたりの生成回数の上限（初回 + 差し戻し2回）

ELEMENT_RULES = [
    # (要素, 説明)
    ('result', '常に含める'),
    ('reasoning', f'最も弱いノードの定着度 < {DETAIL_THRESHOLD}'),
    ('definition_review', f'最も弱いノードの定着度 < {DEFINITION_REVIEW_THRESHOLD}'),
    ('similar_check', f'最も弱いノードの定着度 < {SIMILAR_CHECK_THRESHOLD}'),
    ('error_caution', f'誤りの記録があるノードの定着度 < {CAUTION_CONFIDENCE_LIMIT}'),
]

ELEMENT_LABELS = {
    'result': '結果と使う公式',
    'reasoning': '途中の式変形を省略しない説明',
    'definition_review': '前提知識の定義・公式の意味の復習',
    'similar_check': '類題での確認',
    'error_caution': 'その生徒の誤りの傾向への注意',
}

MASTER_ELEMENTS = ['result', 'reasoning', 'definition_review']


class ExplanationError(ValueError):
    pass


def decide_step(step: Dict[str, Any], node_states: Dict[str, Any],
                error_tendencies: Dict[str, int]) -> Dict[str, Any]:
    """ステップに含める要素を，定着度の数値に対する条件分岐で決める（LLM は呼ばない）。"""
    labels = graph.node_labels()
    nodes = step.get('nodes') or []
    if not nodes:
        return {'elements': ['result'], 'weakest_node': None, 'confidence': None, 'evidence_count': None,
                'node_confidences': {}, 'cautions': [],
                'reasons': ['依存する前提知識ノードがないため，結果と使う公式だけを簡潔に書く']}

    confidences = {n: confidence_of(node_states, n) for n in nodes}
    weakest = min(nodes, key=lambda n: confidences[n][0])
    c, evidence = confidences[weakest]
    weakest_desc = f'{weakest}（{labels.get(weakest, weakest)}）の定着度 {c:.2f}（根拠 {evidence} 問）'

    elements = ['result']
    reasons: List[str] = []
    if c < DETAIL_THRESHOLD:
        elements.append('reasoning')
        reasons.append(f'{weakest_desc} が閾値 {DETAIL_THRESHOLD} を下回るため，途中の式変形を省略せずに書く')
    else:
        reasons.append(f'依存するノードの定着度がすべて {DETAIL_THRESHOLD} 以上（最小は {weakest_desc}）のため，'
                       '結果と使う公式だけを簡潔に書く')
    if c < DEFINITION_REVIEW_THRESHOLD:
        elements.append('definition_review')
        reasons.append(f'定着度 {c:.2f} < {DEFINITION_REVIEW_THRESHOLD} のため，前提知識の定義・公式の意味を復習する')
    if c < SIMILAR_CHECK_THRESHOLD:
        elements.append('similar_check')
        reasons.append(f'定着度 {c:.2f} < {SIMILAR_CHECK_THRESHOLD} のため，類題での確認を添える')

    cautions = []
    for node in nodes:
        count = int(error_tendencies.get(node, 0))
        node_c = confidences[node][0]
        if count >= 1 and node_c < CAUTION_CONFIDENCE_LIMIT:
            cautions.append({'node': node, 'label': labels.get(node, node), 'error_count': count})
            reasons.append(f'{node}（{labels.get(node, node)}）で誤りの記録が {count} 回あり，'
                           f'定着度 {node_c:.2f} < {CAUTION_CONFIDENCE_LIMIT} のため，誤りへの注意を添える')
    if cautions:
        elements.append('error_caution')

    return {
        'elements': elements,
        'weakest_node': weakest,
        'confidence': c,
        'evidence_count': evidence,
        'node_confidences': {n: {'confidence': v[0], 'evidence_count': v[1]} for n, v in confidences.items()},
        'cautions': cautions,
        'reasons': reasons,
    }


def master_decision(step: Dict[str, Any]) -> Dict[str, Any]:
    return {'elements': list(MASTER_ELEMENTS), 'weakest_node': None, 'confidence': None, 'evidence_count': None,
            'node_confidences': {}, 'cautions': [],
            'reasons': ['マスター解説: 生徒によらず行間を全て埋めて書く']}


def plan_explanation(steps: List[Dict[str, Any]], node_states: Dict[str, Any],
                     error_tendencies: Dict[str, int]) -> List[Dict[str, Any]]:
    return [{'step_id': s['id'], 'title': s['title'], 'nodes': s.get('nodes', []),
             **decide_step(s, node_states, error_tendencies)} for s in steps]


# ---------------------------------------------------------------- Chain 1: 解答ステップへの分解

CHAIN1_SYSTEM = (
    'あなたは高等学校数学の解答を，誰にとっても省略できない解答ステップに分解します。'
    '分解は生徒によらない共通のものです。各ステップには，そのステップが依存する前提知識を，'
    '与えられた選択肢の中からだけ選んで付けます（依存するものがなければ空にします）。'
    'ステップは解答の順に並べ，各ステップの結果（得られる式や値）を目安として書きます。'
)


def _chain1_schema(unit: str) -> Dict[str, Any]:
    return {
        'type': 'object',
        'properties': {
            'steps': {
                'type': 'array',
                'items': {
                    'type': 'object',
                    'properties': {
                        'title': {'type': 'string'},
                        'description': {'type': 'string'},
                        'result': {'type': 'string'},
                        'nodes': {'type': 'array', 'items': {'type': 'string', 'enum': graph.node_ids(unit)}},
                    },
                    'required': ['title', 'description', 'result', 'nodes'],
                    'additionalProperties': False,
                },
            },
        },
        'required': ['steps'],
        'additionalProperties': False,
    }


def validate_steps(steps: Any, unit: str) -> List[Dict[str, Any]]:
    """Chain 1 の出力や教員が編集したステップを検証し，id を 1 から振り直す。"""
    if not isinstance(steps, list) or not steps:
        raise ExplanationError('解答ステップが1つ以上必要です')
    known = set(graph.node_ids(unit))
    normalized = []
    for index, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            raise ExplanationError(f'ステップ{index}: オブジェクトである必要があります')
        title = str(step.get('title', '')).strip()
        if not title:
            raise ExplanationError(f'ステップ{index}: title が空です')
        nodes = step.get('nodes') or []
        if not isinstance(nodes, list):
            raise ExplanationError(f'ステップ{index}: nodes はノード名の配列である必要があります')
        nodes = [str(n).strip() for n in nodes]
        unknown = [n for n in nodes if n not in known]
        if unknown:
            raise ExplanationError(f'ステップ{index}: 単元 {unit} に無いノード {", ".join(unknown)} が含まれています')
        normalized.append({
            'id': index,
            'title': title[:200],
            'description': str(step.get('description', '')).strip()[:2000],
            'result': str(step.get('result', '')).strip()[:500],
            'nodes': list(dict.fromkeys(nodes)),
        })
    return normalized


def decompose_problem(llm: LLMClient, unit: str, problem_text: str) -> List[Dict[str, Any]]:
    labels = graph.node_labels()
    options = '\n'.join(f'- {n}: {labels[n]}' for n in graph.node_ids(unit))
    user = (f'単元: {graph.get_unit(unit)["label"]}\n\n問題文:\n{problem_text}\n\n'
            f'前提知識の選択肢（ノード名: 内容）:\n{options}')
    out = llm.generate_json(system=CHAIN1_SYSTEM, user=user, schema=_chain1_schema(unit),
                            schema_name='solution_steps', temperature=0.0)
    return validate_steps(out.get('steps'), unit)


# ---------------------------------------------------------------- Chain 2: 決めた方針で説明を書く

CHAIN2_SYSTEM = (
    'あなたは高等学校数学の解答解説を，解答ステップごとに書きます。指示された「含める要素」だけに'
    '従って書き，方針を変えたり指示にない要素を足したりしないでください。'
    '計算は必ず「左辺 = 右辺」の形で書きます。生徒に語りかける「です・ます」調で，見出しは付けません。'
)


def _element_instructions(decision: Dict[str, Any], step: Dict[str, Any]) -> List[str]:
    labels = graph.node_labels()
    elements = decision['elements']
    lines = ['このステップで得られる結果と，使う公式・考え方を示す。']
    if 'reasoning' in elements:
        lines.append('途中の式変形を省略せず，一行ごとに何をしているかを添えて書く。')
    else:
        lines.append('結果と使う公式だけを簡潔に書く（2〜3文程度）。途中の式変形の詳しい説明は書かない。')
    if 'definition_review' in elements:
        node_text = '，'.join(labels.get(n, n) for n in step.get('nodes', []))
        lines.append(f'最初に，このステップが依存する前提知識（{node_text}）の定義や公式の意味を短く復習する。')
    for caution in decision.get('cautions', []):
        lines.append(f'この生徒は「{caution["label"]}」で誤りの記録が{caution["error_count"]}回ある。'
                     'その誤りが起きやすい箇所に，混同しないための注意を一言添える。')
    if 'similar_check' in elements:
        lines.append('最後に，理解を確かめる簡単な類題を1問，答えを付けて示す。')
    return lines


def write_step(llm: LLMClient, problem: Dict[str, Any], steps: List[Dict[str, Any]], step: Dict[str, Any],
               decision: Dict[str, Any], previous_texts: List[str], feedback: Optional[str] = None) -> str:
    labels = graph.node_labels()
    outline = '\n'.join(f'{s["id"]}. {s["title"]}' for s in steps)
    previous = '\n\n'.join(f'(ステップ{i}) {t}' for i, t in enumerate(previous_texts, start=1)) or '（なし）'
    instructions = '\n'.join(f'- {line}' for line in _element_instructions(decision, step))
    node_text = '，'.join(labels.get(n, n) for n in step.get('nodes', [])) or '（なし）'
    user = (
        f'問題文:\n{problem["problem_text"]}\n\n解答ステップの全体:\n{outline}\n\n'
        f'これまでのステップの説明:\n{previous}\n\n'
        f'今回書くステップ: {step["id"]}. {step["title"]}\n内容: {step.get("description", "")}\n'
        f'結果の目安: {step.get("result", "")}\n依存する前提知識: {node_text}\n\n'
        f'含める要素（この指示だけに従う）:\n{instructions}'
    )
    if feedback:
        user += f'\n\n前回書いた説明には次の誤りがありました。誤りを直して，このステップを書き直してください。\n{feedback}'
    return llm.generate_text(system=CHAIN2_SYSTEM, user=user, temperature=0.3).strip()


# ---------------------------------------------------------------- Chain 4: 計算の抜き出しと検算

EXTRACTION_SYSTEM = (
    'あなたは解説文から計算の主張を抜き出し，数式処理システム（sympy）で扱える式に書き換えます。'
    '書かれている値を直したり計算をやり直したりしてはいけません。誤っていても書かれたとおりに抜き出します。'
)

EXTRACTION_RULES = (
    '- 解説文中の「左辺 = 右辺」の形の計算を，書かれた順に全て抜き出す。\n'
    '- 連続した等式 a = b = c は，a = b と b = c の2つに分ける。\n'
    '- lhs・rhs は sympy で解釈できる表記にする（掛け算 *，累乗 ^，平方根 sqrt()，角度は sin(30°) のように°を付ける，'
    '変数は半角英字。θ は t に置き換える）。lhs_text・rhs_text には解説中の元の表記を入れる。\n'
    '- 文字を定義する式（D = b^2 - 4ac など）もそのまま含める。不等式は含めない。\n'
    '- final_answer には，解説が問題の最終的な答えとして述べている内容を書く（無ければ空文字）。'
)

_EXTRACTION_SCHEMA = {
    'type': 'object',
    'properties': {
        'claims': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'lhs': {'type': 'string'}, 'rhs': {'type': 'string'},
                    'lhs_text': {'type': 'string'}, 'rhs_text': {'type': 'string'},
                },
                'required': ['lhs', 'rhs', 'lhs_text', 'rhs_text'],
                'additionalProperties': False,
            },
        },
        'final_answer': {'type': 'string'},
    },
    'required': ['claims', 'final_answer'],
    'additionalProperties': False,
}


def extract_claims(llm: LLMClient, step_text: str) -> Dict[str, Any]:
    user = f'抜き出しの規則:\n{EXTRACTION_RULES}\n\n解説文:\n{step_text}'
    out = llm.generate_json(system=EXTRACTION_SYSTEM, user=user, schema=_EXTRACTION_SCHEMA,
                            schema_name='calculation_claims', temperature=0.0)
    claims = [c for c in out.get('claims', []) if isinstance(c, dict) and c.get('lhs') and c.get('rhs')]
    return {'claims': claims, 'final_answer': str(out.get('final_answer', '')).strip()}


def verify_step(step_id: int, extracted: Dict[str, Any], definitions: Dict[str, str],
                correct_answer: Optional[str] = None) -> Dict[str, Any]:
    """抜き出した計算を検算する。definitions はこのステップで定義された文字で更新される。"""
    results = []
    for claim in extracted['claims']:
        checked = check_claim(claim['lhs'], claim['rhs'], definitions)
        if checked['status'] == 'definition':
            definitions[str(claim['lhs']).strip()] = str(claim['rhs']).strip()
        results.append({**claim, **checked})

    feedback_lines = [
        f'ステップ{step_id}の「{r.get("lhs_text") or r["lhs"]} = {r.get("rhs_text") or r["rhs"]}」は誤りです'
        f'（正しくは {r["true_value"]}）。'
        for r in results if r['status'] == 'ng'
    ]
    final_check = None
    if correct_answer:
        final_answer = extracted.get('final_answer', '')
        if final_answer:
            verdict = judge_answer(correct_answer, final_answer)
            final_check = {'final_answer': final_answer, 'correct_answer': correct_answer, **verdict}
            if not verdict['is_correct']:
                feedback_lines.append(f'結論の「{final_answer}」は正しい答え「{correct_answer}」と一致しません。')
        else:
            final_check = {'final_answer': '', 'correct_answer': correct_answer, 'is_correct': None,
                           'method': 'not_found', 'detail': '最終的な答えを抜き出せませんでした'}

    counts = {s: sum(1 for r in results if r['status'] == s) for s in ('ok', 'ng', 'definition', 'unverifiable')}
    return {
        'ok': not feedback_lines,
        'claims': results,
        'counts': counts,
        'final_check': final_check,
        'feedback': '\n'.join(feedback_lines) if feedback_lines else None,
    }


# ---------------------------------------------------------------- 生成の全体（Chain 2〜4）

def _assemble(problem: Dict[str, Any], steps: List[Dict[str, Any]], texts: List[str]) -> str:
    """Chain 3: 各ステップの説明を順に結合する。"""
    parts = [f'# {problem["title"]}']
    for step, text in zip(steps, texts):
        parts.append(f'## ステップ{step["id"]}　{step["title"]}\n\n{text}')
    return '\n\n'.join(parts) + '\n'


def generate_explanation(llm: LLMClient, problem: Dict[str, Any], node_states: Optional[Dict[str, Any]] = None,
                         error_tendencies: Optional[Dict[str, int]] = None, kind: str = 'individual') -> Dict[str, Any]:
    """積み上げ型の個別解説（kind='individual'）またはマスター解説（kind='master'）を生成する。"""
    steps = problem['steps']
    if not steps:
        raise ExplanationError('解答ステップがありません（先に Chain 1 を実行してください）')
    if kind == 'master':
        decisions = [{'step_id': s['id'], 'title': s['title'], 'nodes': s.get('nodes', []), **master_decision(s)}
                     for s in steps]
    else:
        decisions = plan_explanation(steps, node_states or {}, error_tendencies or {})

    definitions: Dict[str, str] = {}
    texts: List[str] = []
    step_traces = []
    warnings: List[str] = []
    for index, (step, decision) in enumerate(zip(steps, decisions)):
        is_last = index == len(steps) - 1
        feedback = None
        attempts = []
        text = ''
        verification: Dict[str, Any] = {}
        for attempt in range(1, MAX_ATTEMPTS + 1):
            text = write_step(llm, problem, steps, step, decision, texts, feedback)
            step_definitions = dict(definitions)
            try:
                extracted = extract_claims(llm, text)
                verification = verify_step(step['id'], extracted, step_definitions,
                                           problem.get('correct_answer') if is_last else None)
            except Exception as exc:  # 抜き出しの失敗は検算できなかったものとして扱う
                extracted = {'claims': [], 'final_answer': ''}
                verification = {'ok': True, 'claims': [], 'counts': {}, 'final_check': None, 'feedback': None,
                                'error': f'計算の抜き出しに失敗しました（{exc.__class__.__name__}）'}
            attempts.append({'attempt': attempt, 'text': text, **verification})
            if verification['ok']:
                definitions = step_definitions
                break
            feedback = verification['feedback']
        step_status = 'ok' if verification.get('ok') else 'needs_review'
        step_warnings = []
        if not verification.get('claims'):
            step_warnings.append(f'ステップ{step["id"]}: 計算が一つも抜き出されなかったため，このステップは検算されていません')
        if verification.get('error'):
            step_warnings.append(f'ステップ{step["id"]}: {verification["error"]}')
        if step_status == 'needs_review':
            step_warnings.append(f'ステップ{step["id"]}: 再生成の上限（{MAX_ATTEMPTS}回）に達したため，教員の確認が必要です')
        warnings.extend(step_warnings)
        texts.append(text)
        step_traces.append({
            'step_id': step['id'], 'title': step['title'], 'nodes': step.get('nodes', []),
            'decision': decision, 'text': text, 'status': step_status,
            'attempt_count': len(attempts), 'attempts': attempts, 'warnings': step_warnings,
        })

    return {
        'kind': kind,
        'status': 'ok' if all(s['status'] == 'ok' for s in step_traces) else 'needs_review',
        'content': _assemble(problem, steps, texts),
        'steps': step_traces,
        'warnings': warnings,
        'model_name': getattr(llm, 'model_name', None),
    }


def build_subtractive(problem: Dict[str, Any], master: Dict[str, Any], node_states: Dict[str, Any],
                      error_tendencies: Dict[str, int]) -> Dict[str, Any]:
    """引き算型の解説（比較用 (B)）: マスター解説から，定着済みのステップを結果だけに縮める。LLM は呼ばない。"""
    steps = problem['steps']
    master_texts = {s['step_id']: s['text'] for s in master['steps']}
    decisions = plan_explanation(steps, node_states, error_tendencies)
    texts, traces = [], []
    for step, decision in zip(steps, decisions):
        keep = 'reasoning' in decision['elements']
        text = master_texts.get(step['id'], '') if keep else (step.get('result') or '（省略）')
        texts.append(text)
        traces.append({'step_id': step['id'], 'title': step['title'], 'nodes': step.get('nodes', []),
                       'decision': decision, 'text': text, 'status': 'ok',
                       'kept_from_master': keep, 'attempt_count': 0, 'attempts': [], 'warnings': []})
    return {'kind': 'subtractive', 'status': master['status'], 'content': _assemble(problem, steps, texts),
            'steps': traces, 'warnings': [], 'model_name': master.get('model_name')}


# ---------------------------------------------------------------- 比較の指標（5.5節 表10）

def text_similarity(a: str, b: str) -> float:
    """文字列の一致度（difflib）。"""
    return round(difflib.SequenceMatcher(None, a, b).ratio(), 3)


def cosine_similarity(a: List[float], b: List[float]) -> Optional[float]:
    """埋め込みベクトルのコサイン類似度（意味の類似度）。"""
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if not norm_a or not norm_b or len(a) != len(b):
        return None
    return round(sum(x * y for x, y in zip(a, b)) / (norm_a * norm_b), 3)


def explanation_metrics(trace: Dict[str, Any], node_states: Dict[str, Any]) -> Dict[str, Any]:
    """未定着の箇所を補えているか・定着済みの箇所が冗長でないかを，ステップと前提知識の対応から算出する。"""
    weak_steps, mastered_steps = [], []
    for step in trace['steps']:
        nodes = step.get('nodes') or []
        if not nodes:
            continue
        if any(confidence_of(node_states, n)[0] < DETAIL_THRESHOLD for n in nodes):
            weak_steps.append(step)
        else:
            mastered_steps.append(step)
    detailed = [s for s in weak_steps if 'reasoning' in s['decision']['elements'] or s.get('kept_from_master')]
    return {
        'weak_step_count': len(weak_steps),
        'weak_steps_detailed_ratio': round(len(detailed) / len(weak_steps), 3) if weak_steps else None,
        'mastered_step_count': len(mastered_steps),
        'mastered_steps_chars': sum(len(s['text']) for s in mastered_steps),
        'total_chars': sum(len(s['text']) for s in trace['steps']),
    }
