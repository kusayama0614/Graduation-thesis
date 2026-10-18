"""
数学的検証チェーンの②の使い方：生成した解説（Chain 2 の出力）の検算。

答案判定（verify_chain.py）と同じ「判定層」（sympy による同値判定）を使う。
違うのは入力で、解説文から LLM が抜き出した「計算の主張」（左辺 = 右辺）の一覧を受け取る。
判定（OK/NG）は sympy が決め、LLM には任せない。NG があれば、どのステップのどの主張が
誤りか、正しい値は何かを添えて Chain 2 に差し戻す。

この段階では「主張の抽出（LLM）」はまだ実装しておらず、抽出結果の形（JSON）を手で与えて
判定層とフィードバック文の生成だけを動かしている。
"""
import sympy as sp

deg = sp.pi / 180


def sym(s):
    return sp.sympify(s, locals={"deg": deg})


def check_claim(claim):
    """claim = {"step_id", "lhs", "rhs"}（sympy で解釈できる文字列）"""
    lhs, rhs = sym(claim["lhs"]), sym(claim["rhs"])
    ok = sp.simplify(lhs - rhs) == 0
    return {**claim, "ok": ok, "true_value": sp.nsimplify(sp.simplify(lhs))}


def verify_explanation(claims, max_retry=3, retry=0):
    results = [check_claim(c) for c in claims]
    ng = [r for r in results if not r["ok"]]
    if not ng:
        return {"status": "OK", "results": results}
    if retry + 1 >= max_retry:
        return {"status": "ESCALATE", "results": results,
                "message": "再生成の上限に達したため、教員の確認に回す"}
    feedback = "\n".join(
        f"ステップ{r['step_id']}の「{r['lhs_text']} = {r['rhs_text']}」は誤りです"
        f"（正しくは {r['true_value']}）。このステップを書き直してください。"
        for r in ng)
    return {"status": "NG", "results": results, "feedback_to_chain2": feedback}


if __name__ == "__main__":
    # 例：sin150° の解説から抽出された主張（手で作成した例。1つ目は正しい解説、2つ目は符号を誤った解説）
    good = [
        {"step_id": 1, "lhs": "sin(150*deg)", "rhs": "sin(30*deg)", "lhs_text": "sin150°", "rhs_text": "sin30°"},
        {"step_id": 2, "lhs": "sin(30*deg)", "rhs": "1/2", "lhs_text": "sin30°", "rhs_text": "1/2"},
    ]
    bad = [
        {"step_id": 1, "lhs": "cos(150*deg)", "rhs": "cos(30*deg)", "lhs_text": "cos150°", "rhs_text": "cos30°"},
        {"step_id": 2, "lhs": "cos(30*deg)", "rhs": "sqrt(3)/2", "lhs_text": "cos30°", "rhs_text": "√3/2"},
    ]
    for name, claims in [("正しい解説", good), ("符号を誤った解説", bad)]:
        out = verify_explanation(claims)
        print(f"[{name}] 判定: {out['status']}")
        for r in out["results"]:
            print(f"   ステップ{r['step_id']}: {r['lhs_text']} = {r['rhs_text']}  → {'○' if r['ok'] else '×'}")
        if out["status"] == "NG":
            print("   Chain 2 への差し戻し文:\n   " + out["feedback_to_chain2"].replace("\n", "\n   "))
