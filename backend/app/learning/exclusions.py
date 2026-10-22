"""研究協力不可の生徒（ResearchExclusion）。

登録した生徒の解答解説は作らない: 解説の生成・文章を整える・要確認の手直し・冊子・TeX・PDF の対象から外す。
登録するときに，既に作った解説を消すこともできる（同じ解説を使い回していた生徒の分は残す）。
"""
from __future__ import annotations

import re
from typing import Iterable, List, Optional, Set

_ID = re.compile(r'^\s*(\d+)\s*-\s*(\d+)\s*-\s*(\d+)\s*$')
_JAPANESE = re.compile(r'^\s*(\d+)\s*年\s*(\d+)\s*組\s*(\d+)\s*番?\s*$')


def parse_student_id(text: str) -> Optional[str]:
    """「1-8-15」「1年8組15番」を生徒ID「1-8-15」にする。読めなければ None。"""
    text = str(text or '').translate(str.maketrans('０１２３４５６７８９－ー―', '0123456789---'))
    match = _ID.match(text) or _JAPANESE.match(text)
    if not match:
        return None
    return '-'.join(str(int(part)) for part in match.groups())


def excluded_ids(teacher_id: str) -> Set[str]:
    from app.models import ResearchExclusion

    return {row.student_id for row in ResearchExclusion.query.filter_by(teacher_id=teacher_id).all()}


def without_excluded(teacher_id: str, student_ids: Iterable[str]) -> List[str]:
    excluded = excluded_ids(teacher_id)
    return [sid for sid in student_ids if sid not in excluded]


def delete_explanations(teacher_id: str, student_id: str) -> int:
    """その生徒の解説を消す。ほかの生徒が使い回していた元の解説なら，使い回していた生徒の1件を新しい元にする。"""
    from app import db
    from app.models import ExplanationEmbedding, GeneratedExplanation

    rows = GeneratedExplanation.query.filter_by(teacher_id=teacher_id, student_id=student_id).all()
    for row in rows:
        dependents = (GeneratedExplanation.query.filter_by(teacher_id=teacher_id, reused_from=row.id)
                      .filter(GeneratedExplanation.student_id != student_id)
                      .order_by(GeneratedExplanation.created_at.asc()).all())
        if dependents:
            new_root = dependents[0]
            new_root.reused_from = None
            for other in dependents[1:]:
                other.reused_from = new_root.id
        ExplanationEmbedding.query.filter_by(explanation_id=row.id).delete()
        db.session.delete(row)
    db.session.commit()
    return len(rows)
