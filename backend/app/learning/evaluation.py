"""評価の集計（卒業論文 5.4節・5.5節）

- 数学的整合率（5.4節(4)）: 生成した解説について，(a) 最初の生成で全ての計算が正しかった割合，
  (b) 差し戻しによって修正された割合，(c) 上限に達して教員の確認に回った割合。
- 誤りの種類の診断の一致率（5.4節(3)）: 研究者のラベルと LLM の診断の一致率と Cohen の κ。
- 表10の比較の行: 未定着の箇所を補えたか・定着済みの箇所の分量・固有の内容（誤りへの注意）・類似度。

いずれも記録（trace）から決定的に計算し，LLM は呼ばない。
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from app.learning import explanation as chain

# 整合率の対象（LLM で生成・検算した解説）
GENERATED_KINDS = ('individual', 'master')


def _rate(part: int, whole: int) -> Optional[float]:
    return round(part / whole, 3) if whole else None


def _final_attempt(step: Dict[str, Any]) -> Dict[str, Any]:
    attempts = step.get('attempts') or []
    return attempts[-1] if attempts else {}


def step_outcome(step: Dict[str, Any]) -> str:
    """first_ok（初回で検算OK）/ fixed（差し戻しで修正）/ needs_review（上限に達した）"""
    if step.get('status') != 'ok':
        return 'needs_review'
    return 'first_ok' if (step.get('attempt_count') or 1) <= 1 else 'fixed'


def explanation_outcome(trace: Dict[str, Any]) -> str:
    outcomes = [step_outcome(s) for s in trace.get('steps', [])]
    if 'needs_review' in outcomes or trace.get('status') == 'needs_review':
        return 'needs_review'
    return 'fixed' if 'fixed' in outcomes else 'first_ok'


def consistency(traces: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """数学的整合率。traces は generate_explanation の戻り値（または保存した trace）の並び。"""
    explanations = Counter()
    steps = Counter()
    by_kind: Dict[str, Counter] = {}
    checked_claims = ng_claims = checked_relations = ng_relations = unverified_steps = 0
    total = 0
    for trace in traces:
        kind = trace.get('kind', 'individual')
        if kind not in GENERATED_KINDS:
            continue
        total += 1
        outcome = explanation_outcome(trace)
        explanations[outcome] += 1
        by_kind.setdefault(kind, Counter())[outcome] += 1
        for step in trace.get('steps', []):
            steps[step_outcome(step)] += 1
            final = _final_attempt(step)
            claims = [c for c in final.get('claims', []) if c.get('status') in ('ok', 'ng')]
            relations = [r for r in final.get('relations', []) if r.get('status') in ('ok', 'ng')]
            checked_claims += len(claims)
            checked_relations += len(relations)
            if not claims and not relations:
                unverified_steps += 1
            # 差し戻しのきっかけになった誤り（全ての回の ng）を数える
            for attempt in step.get('attempts') or []:
                ng_claims += sum(1 for c in attempt.get('claims', []) if c.get('status') == 'ng')
                ng_relations += sum(1 for r in attempt.get('relations', []) if r.get('status') == 'ng')

    step_total = sum(steps.values())

    def rates(counter: Counter, whole: int) -> Dict[str, Any]:
        return {key: {'count': counter.get(key, 0), 'rate': _rate(counter.get(key, 0), whole)}
                for key in ('first_ok', 'fixed', 'needs_review')}

    return {
        'explanation_count': total,
        'explanations': rates(explanations, total),
        'by_kind': {kind: {'count': sum(c.values()), **rates(c, sum(c.values()))} for kind, c in by_kind.items()},
        'step_count': step_total,
        'steps': rates(steps, step_total),
        'checked_claims': checked_claims,
        'checked_relations': checked_relations,
        'ng_claims': ng_claims,
        'ng_relations': ng_relations,
        'unverified_steps': unverified_steps,
    }


# ---------------------------------------------------------------- 一致率（5.4節(3)）

def cohen_kappa(pairs: Sequence[Tuple[str, str]]) -> Optional[float]:
    n = len(pairs)
    if not n:
        return None
    observed = sum(1 for a, b in pairs if a == b) / n
    left = Counter(a for a, _ in pairs)
    right = Counter(b for _, b in pairs)
    expected = sum(left[k] * right.get(k, 0) for k in left) / (n * n)
    if expected >= 1:
        return 1.0 if observed == 1 else None
    return round((observed - expected) / (1 - expected), 3)


def agreement(pairs: Sequence[Tuple[str, str]]) -> Dict[str, Any]:
    """pairs は (正解ラベル, 予測) の並び。"""
    agree = sum(1 for a, b in pairs if a == b)
    confusion: Dict[str, Dict[str, int]] = {}
    for label, predicted in pairs:
        row = confusion.setdefault(label, {})
        row[predicted] = row.get(predicted, 0) + 1
    return {
        'n': len(pairs),
        'agree': agree,
        'accuracy': _rate(agree, len(pairs)),
        'kappa': cohen_kappa(pairs),
        'confusion': confusion,
    }


# ---------------------------------------------------------------- 表10の比較（5.5節）

def unique_content(trace: Dict[str, Any]) -> Dict[str, Any]:
    """マスター解説に含めない要素（誤りへの注意・類題）を含めたステップ。固有の内容の機械的な目安。"""
    caution = [s['step_id'] for s in trace.get('steps', []) if 'error_caution' in s['decision']['elements']]
    similar = [s['step_id'] for s in trace.get('steps', []) if 'similar_check' in s['decision']['elements']]
    return {'caution_steps': caution, 'similar_check_steps': similar}


def table10_row(student_id: Optional[str], kind: str, trace: Dict[str, Any], content: str,
                node_states: Dict[str, Any], master_content: Optional[str] = None,
                semantic_to_master: Optional[float] = None) -> Dict[str, Any]:
    metrics = chain.explanation_metrics(trace, node_states)
    # マスター解説は生徒によらないので，固有の内容は積み上げ型だけで数える
    unique = unique_content(trace) if kind == 'individual' else {'caution_steps': [], 'similar_check_steps': []}
    return {
        'student_id': student_id,
        'kind': kind,
        'status': trace.get('status'),
        'outcome': explanation_outcome(trace) if kind in GENERATED_KINDS else None,
        **metrics,
        'caution_steps': unique['caution_steps'],
        'similar_check_steps': unique['similar_check_steps'],
        'text_sim_to_master': (chain.text_similarity(content or '', master_content)
                               if master_content is not None and kind != 'master' else None),
        'semantic_sim_to_master': semantic_to_master if kind != 'master' else None,
    }


def pairwise_similarity(contents: Dict[str, str]) -> List[Dict[str, Any]]:
    """生徒どうしの文字列の一致度（同じ方式の解説の間）。"""
    keys = sorted(contents)
    rows = []
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            rows.append({'a': a, 'b': b, 'text_sim': chain.text_similarity(contents[a], contents[b])})
    return rows


def mean(values: Iterable[Optional[float]]) -> Optional[float]:
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), 3) if values else None
