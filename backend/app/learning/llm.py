"""LLM 呼び出しの抽象化

本システムで LLM が担うのは，(a) 構造化された出力（Chain 1 の分解，誤りの診断，計算の抜き出し）と
(b) 決められた方針に沿った文章の作成（Chain 2）だけである。正誤や詳しさの決定は LLM に任せない。

テストや API キーのない環境では，Flask の設定 `LEARNING_LLM_CLIENT` に同じインターフェースを持つ
オブジェクトを入れて差し替える。
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional, Protocol

from flask import current_app

DEFAULT_MODEL = 'gpt-4o-mini'


class LLMUnavailable(RuntimeError):
    """LLM が設定されていない（OPENAI_API_KEY が無い）。"""


class LLMClient(Protocol):
    model_name: str

    def generate_json(self, *, system: str, user: str, schema: Dict[str, Any],
                      schema_name: str, temperature: float = 0.0) -> Dict[str, Any]:
        """JSON Schema（strict）に従った出力を dict で返す。"""

    def generate_text(self, *, system: str, user: str, temperature: float = 0.3) -> str:
        """自由記述の文章を返す。"""


class OpenAIChatClient:
    """LangChain の ChatOpenAI を用いた実装。"""

    def __init__(self, api_key: str, model_name: Optional[str] = None):
        self.api_key = api_key
        self.model_name = model_name or os.getenv('OPENAI_MODEL', '').strip() or DEFAULT_MODEL

    def _chat(self, temperature: float):
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(model=self.model_name, api_key=self.api_key, temperature=temperature)

    def generate_json(self, *, system, user, schema, schema_name, temperature=0.0):
        structured = self._chat(temperature).with_structured_output(
            {'name': schema_name, 'schema': schema, 'strict': True},
            method='json_schema',
        )
        return structured.invoke([('system', system), ('human', user)])

    def generate_text(self, *, system, user, temperature=0.3):
        message = self._chat(temperature).invoke([('system', system), ('human', user)])
        return str(message.content)


def get_llm_client(required: bool = False) -> Optional[LLMClient]:
    client = current_app.config.get('LEARNING_LLM_CLIENT')
    if client is not None:
        return client
    api_key = os.getenv('OPENAI_API_KEY', '').strip()
    if api_key:
        return OpenAIChatClient(api_key)
    if required:
        raise LLMUnavailable('OPENAI_API_KEY が設定されていないため，LLM を使う処理を実行できません')
    return None
