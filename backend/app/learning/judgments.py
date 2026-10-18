"""演習の小問ごとの判定ラベル（卒業論文 表2「判定結果」，表12）"""

CORRECT = 'correct'                        # 正解
CORRECT_FORM_ISSUE = 'correct_form_issue'  # 正解だが書き方に問題
WRONG = 'wrong'                            # 誤答
INCOMPLETE = 'incomplete'                  # 不完全（組の答えの一部しかない）
FORM_ISSUE = 'form_issue'                  # 書き方の問題により誤り
BLANK = 'blank'                            # 空欄
UNREADABLE = 'unreadable'                  # 判読不能（集計から除外）
UNMARKED = 'unmarked'                      # 判定なし（集計から除外）

ALL_JUDGMENTS = (CORRECT, CORRECT_FORM_ISSUE, WRONG, INCOMPLETE, FORM_ISSUE, BLANK, UNREADABLE, UNMARKED)
CORRECT_JUDGMENTS = frozenset({CORRECT, CORRECT_FORM_ISSUE})
ERROR_JUDGMENTS = frozenset({WRONG, INCOMPLETE, FORM_ISSUE})  # 誤りの種類を診断する対象
EXCLUDED_JUDGMENTS = frozenset({UNREADABLE, UNMARKED})

# 既存の教員確認結果（correct / incorrect / unmarked）との対応
RESULT_OF_JUDGMENT = {
    CORRECT: 'correct', CORRECT_FORM_ISSUE: 'correct',
    WRONG: 'incorrect', INCOMPLETE: 'incorrect', FORM_ISSUE: 'incorrect', BLANK: 'incorrect',
    UNREADABLE: 'unmarked', UNMARKED: 'unmarked',
}
DEFAULT_JUDGMENT_OF_RESULT = {'correct': CORRECT, 'incorrect': WRONG, 'unmarked': UNMARKED}


def judgment_from_method(method: str) -> str:
    """math_verification.judge_answer の method を判定ラベルに対応付ける。"""
    if method in ('symbolic_equal', 'numeric_equal', 'set_equal', 'list_equal', 'string_exact'):
        return CORRECT
    if method == 'incomplete':
        return INCOMPLETE
    if method == 'empty':
        return BLANK
    return WRONG
