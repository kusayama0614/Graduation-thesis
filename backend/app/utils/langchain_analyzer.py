# ==================== LangChain 分析ユーティリティ ====================
"""
LangChain を用いた AI 分析・レポート生成モジュール
"""
import os
from langchain_openai import ChatOpenAI
from langchain_core.prompts import PromptTemplate
from langchain_core.output_parsers import StrOutputParser
import json
from typing import Dict, Any, Optional

class ReportGenerator:
    """レポート生成クラス"""
    
    def __init__(self):
        """初期化"""
        api_key = os.getenv('OPENAI_API_KEY')
        if not api_key:
            raise ValueError("OPENAI_API_KEY is not set in environment variables")

        configured_model = os.getenv('OPENAI_MODEL', '').strip()
        fallback_models = [
            configured_model,
            'gpt-4o-mini',
            'gpt-4.1-mini',
            'gpt-4o',
        ]
        self.model_candidates = []
        for model_name in fallback_models:
            if model_name and model_name not in self.model_candidates:
                self.model_candidates.append(model_name)

        self.api_key = api_key
        self.output_parser = StrOutputParser()

    def _build_llm(self, model_name: str):
        return ChatOpenAI(
            model=model_name,
            openai_api_key=self.api_key,
            temperature=0.7,
            max_tokens=2000
        )

    def _invoke_with_model_fallback(self, prompt_template: PromptTemplate, inputs: Dict[str, Any]) -> str:
        last_error = None

        for model_name in self.model_candidates:
            try:
                llm = self._build_llm(model_name)
                chain = prompt_template | llm | self.output_parser
                return chain.invoke(inputs)
            except Exception as e:
                last_error = e
                error_text = str(e)
                if 'NOT_FOUND' in error_text or 'not found' in error_text:
                    continue
                if 'RESOURCE_EXHAUSTED' in error_text or 'quota' in error_text.lower() or '429' in error_text:
                    return self._generate_local_fallback(prompt_template.template, inputs, error_text)
                raise

        if last_error is not None:
            error_text = str(last_error)
            if 'RESOURCE_EXHAUSTED' in error_text or 'quota' in error_text.lower() or '429' in error_text:
                return self._generate_local_fallback(prompt_template.template, inputs, error_text)

        raise last_error

    def _generate_local_fallback(self, prompt_text: str, inputs: Dict[str, Any], reason: str) -> str:
        student_name = inputs.get('student_name', '未名の生徒')
        subject = inputs.get('subject', '未指定')
        test_name = inputs.get('test_name', '未指定')
        score = inputs.get('score', 0)
        correct_answers = inputs.get('correct_answers', 0)
        total_questions = inputs.get('total_questions', 0) or 1
        accuracy = round((correct_answers / total_questions) * 100, 1)

        if '弱点エリア' in prompt_text:
            weak_areas = inputs.get('weak_areas', '基本概念の理解を優先して見直してください')
            return (
                f"【ローカル生成レポート】\n"
                f"対象: {student_name} / {subject} / {test_name}\n"
                f"スコア: {score}/100\n"
                f"正答率: {accuracy}%\n\n"
                f"OpenAI API がクォータ不足のため利用できませんでした。\n"
                f"そのため、ルールベースで学習計画を生成しています。\n\n"
                f"1. 短期目標（1週間）\n"
                f"- {weak_areas}\n"
                f"- 間違えた問題を1回ずつ解き直す\n"
                f"- 毎日15〜20分の復習を続ける\n\n"
                f"2. 中期目標（1ヶ月）\n"
                f"- 基礎問題での取りこぼしを減らす\n"
                f"- 間違いの原因を「計算」「理解不足」「読み落とし」に分類する\n\n"
                f"3. 学習方法\n"
                f"- 問題文を音読してから解く\n"
                f"- 1問ごとに解き方を短くメモする\n"
                f"- 同じ単元を翌日にもう一度解き直す\n\n"
                f"4. モチベーション維持\n"
                f"- 1日の学習量を小さく区切る\n"
                f"- できたことを記録する\n\n"
                f"5. 次回目標\n"
                f"- 現在より5〜10点の向上を目指す\n\n"
                f"補足: このレポートは OpenAI ではなくローカル生成です。\n"
                f"理由: {reason}"
            )

        return (
            f"【ローカル生成レポート】\n"
            f"対象: {student_name} / {subject} / {test_name}\n"
            f"スコア: {score}/100\n"
            f"正答率: {accuracy}%\n\n"
            f"OpenAI API がクォータ不足のため利用できませんでした。\n"
            f"そのため、ルールベースで分析レポートを生成しています。\n\n"
            f"1. 全体評価\n"
            f"- 得点は一定の到達度を示していますが、安定性の向上が必要です。\n"
            f"- 正答率を基準に、理解が曖昧な単元を洗い出してください。\n\n"
            f"2. 強み\n"
            f"- 基本問題では得点できている可能性があります。\n"
            f"- 取り組みの継続は良い傾向です。\n\n"
            f"3. 改善点\n"
            f"- 間違いの原因を記録する\n"
            f"- 似た問題をまとめて復習する\n"
            f"- 解答後の見直し時間を確保する\n\n"
            f"4. 学習提案\n"
            f"- 毎日1単元ずつ復習する\n"
            f"- 間違えた問題をノートに整理する\n"
            f"- 週末にまとめて再テストする\n\n"
            f"5. 目標設定\n"
            f"- 次回は現状より5〜10点の上積みを狙う\n\n"
            f"補足: このレポートは OpenAI ではなくローカル生成です。\n"
            f"理由: {reason}"
        )

    
    def generate_analysis_report(self, student_data: Dict[str, Any]) -> str:
        """
        生徒の答案データから分析レポートを生成
        
        Args:
            student_data: 生徒のテストデータ（スコア、答案数、正答数等）
        
        Returns:
            生成されたレポート（テキスト）
        """
        
        # プロンプトテンプレート
        prompt_template = PromptTemplate(
            input_variables=["student_name", "subject", "test_name", "score", 
                           "correct_answers", "total_questions", "error_patterns"],
            template="""
あなたは教育のプロフェッショナルです。以下の生徒のテスト結果データに基づいて、
詳細で建設的な分析レポートを日本語で生成してください。

【生徒情報】
- 名前: {student_name}
- 科目: {subject}
- テスト名: {test_name}

【テスト結果】
- スコア: {score}/100
- 正答数: {correct_answers}/{total_questions}

【エラーパターン】
{error_patterns}

以下の項目を含むレポートを生成してください：
1. 全体的な成績評価（スコアの妥当性、到達度）
2. 強み（正答できている領域）
3. 改善が必要な領域
4. 具体的な学習提案（3-5個の実行可能な施策）
5. 次回のテストに向けた目標設定

レポートは構造化され、読みやすい形式で出力してください。
"""
        )
        
        # エラーパターンのテキスト化
        error_text = ""
        if isinstance(student_data.get('error_patterns'), list):
            for pattern in student_data['error_patterns']:
                error_text += f"- {pattern.get('pattern')}: {pattern.get('count')}件\n"
        else:
            error_text = "特に顕著なエラーパターンは検出されていません"
        
        # レポート生成（利用可能な OpenAI モデルを順に試す）
        result = self._invoke_with_model_fallback(prompt_template, {
            'student_name': student_data.get('student_name', '未名の生徒'),
            'subject': student_data.get('subject', '未指定'),
            'test_name': student_data.get('test_name', '未指定'),
            'score': student_data.get('score', 0),
            'correct_answers': student_data.get('correct_answers', 0),
            'total_questions': student_data.get('questions', 20),
            'error_patterns': error_text
        })
        
        return result
    
    def generate_study_plan(self, student_data: Dict[str, Any]) -> str:
        """
        生徒の弱点に基づいた学習計画を生成
        
        Args:
            student_data: 生徒のテストデータ
        
        Returns:
            生成された学習計画（テキスト）
        """
        
        prompt_template = PromptTemplate(
            input_variables=["student_name", "subject", "weak_areas", "score"],
            template="""
あなたは経験豊かな教育アドバイザーです。
以下の生徒に対して、具体的で実行可能な学習計画を日本語で作成してください。

【生徒情報】
- 名前: {student_name}
- 科目: {subject}
- 現在のスコア: {score}/100

【弱点エリア】
{weak_areas}

以下の構成で学習計画を提案してください：

1. 短期目標（1週間）- 改善すべき優先順位
2. 中期目標（1ヶ月）- 段階的な習得計画
3. 学習方法の具体例
   - 使用すべき教材や参考書
   - 実践的な演習方法
   - 理解度確認の方法
4. モチベーション維持のコツ
5. 次回のテストで目指すスコア

実行可能で、生徒が実際に取り組める内容にしてください。
"""
        )
        
        # 弱点エリアのテキスト化
        weak_areas = ""
        if isinstance(student_data.get('weak_areas'), list):
            for area in student_data['weak_areas']:
                weak_areas += f"- {area}\n"
        else:
            weak_areas = "基本概念の理解が不十分な可能性があります"
        
        result = self._invoke_with_model_fallback(prompt_template, {
            'student_name': student_data.get('student_name', '未名の生徒'),
            'subject': student_data.get('subject', '未指定'),
            'weak_areas': weak_areas,
            'score': student_data.get('score', 0)
        })
        
        return result
    
    def generate_feedback(self, student_data: Dict[str, Any]) -> Dict[str, str]:
        """
        複数の観点からのフィードバックを生成
        
        Args:
            student_data: 生徒のテストデータ
        
        Returns:
            複数のフィードバック（辞書形式）
        """
        
        return {
            'analysis': self.generate_analysis_report(student_data),
            'study_plan': self.generate_study_plan(student_data)
        }


def get_report_generator() -> ReportGenerator:
    """レポートジェネレータのシングルトンインスタンスを取得"""
    if not hasattr(get_report_generator, '_instance'):
        get_report_generator._instance = ReportGenerator()
    return get_report_generator._instance
