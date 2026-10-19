"""LLM 呼び出しの抽象化

本システムで LLM が担うのは，(a) 構造化された出力（Chain 1 の分解，誤りの診断，計算の抜き出し）と
(b) 決められた方針に沿った文章の作成（Chain 2）だけである。正誤や詳しさの決定は LLM に任せない。

LLM には Gemini（既定 gemini-3.8-flash）を用いる。設定は app/utils/gemini.py を参照。
テストや API キーのない環境では，Flask の設定 `LEARNING_LLM_CLIENT` に同じインターフェースを持つ
オブジェクトを入れて差し替える。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol

from flask import current_app

from app.utils.gemini import (
    build_chat_model, embed_for_similarity, gemini_api_key, gemini_embedding_model_name, gemini_model_name,
    message_text,
)


class LLMUnavailable(RuntimeError):
    """LLM が設定されていない（GEMINI_API_KEY が無い）。"""


class LLMClient(Protocol):
    model_name: str

    def generate_json(self, *, system: str, user: str, schema: Dict[str, Any],
                      schema_name: str, temperature: float = 0.0,
                      thinking_level: Optional[str] = None) -> Dict[str, Any]:
        """JSON Schema に従った出力を dict で返す。thinking_level で思考の深さを個別に指定できる。"""

    def generate_text(self, *, system: str, user: str, temperature: float = 0.3) -> str:
        """自由記述の文章を返す。"""


class GeminiChatClient:
    """LangChain の ChatGoogleGenerativeAI を用いた実装。

    temperature は Gemini 2.x 以前のモデルにだけ渡す（Gemini 3 以降はモデルの既定値を使う）。
    """

    def __init__(self, api_key: str, model_name: Optional[str] = None):
        self.api_key = api_key
        self.model_name = model_name or gemini_model_name()

    def _chat(self, temperature: float, thinking_level: Optional[str] = None):
        return build_chat_model(self.model_name, api_key=self.api_key, temperature=temperature,
                                thinking_level=thinking_level)

    def generate_json(self, *, system, user, schema, schema_name, temperature=0.0, thinking_level=None):
        # Gemini では JSON Schema が response_json_schema として送られ，出力がスキーマに制約される
        structured = self._chat(temperature, thinking_level).with_structured_output(schema, method='json_schema')
        return structured.invoke([('system', system), ('human', user)])

    def generate_text(self, *, system, user, temperature=0.3):
        return message_text(self._chat(temperature).invoke([('system', system), ('human', user)]))

    def read_image_json(self, *, system, user, image_bytes, mime_type, schema, schema_name, thinking_level=None):
        """画像（スキャンしたページなど）を読み取り，JSON Schema に従った出力を dict で返す。"""
        import base64
        from langchain_core.messages import HumanMessage, SystemMessage

        data_url = f'data:{mime_type};base64,{base64.b64encode(image_bytes).decode("ascii")}'
        message = HumanMessage(content=[{'type': 'text', 'text': user},
                                        {'type': 'image_url', 'image_url': {'url': data_url}}])
        structured = self._chat(0.0, thinking_level).with_structured_output(schema, method='json_schema')
        return structured.invoke([SystemMessage(content=system), message])


def get_llm_client(required: bool = False) -> Optional[LLMClient]:
    client = current_app.config.get('LEARNING_LLM_CLIENT')
    if client is not None:
        return client
    api_key = gemini_api_key()
    if api_key:
        return GeminiChatClient(api_key)
    if required:
        raise LLMUnavailable('GEMINI_API_KEY が設定されていないため，LLM を使う処理を実行できません')
    return None


def get_vision_client(required: bool = False):
    """画像を読み取るクライアント（read_image_json を持つもの）。テストでは `BULK_VISION_CLIENT` で差し替える。"""
    client = current_app.config.get('BULK_VISION_CLIENT')
    if client is not None:
        return client
    api_key = gemini_api_key()
    if api_key:
        return GeminiChatClient(api_key)
    if required:
        raise LLMUnavailable('GEMINI_API_KEY が設定されていないため，プリントの見出しを読み取れません')
    return None


class EmbeddingClient(Protocol):
    model_name: str

    def embed(self, texts: List[str]) -> List[List[float]]:
        """文章ごとの埋め込みベクトルを返す。"""


class GeminiEmbeddingClient:
    """Gemini の埋め込みモデル（既定 gemini-embedding-2）。"""

    def __init__(self, api_key: str, model_name: Optional[str] = None):
        self.api_key = api_key
        self.model_name = model_name or gemini_embedding_model_name()

    def embed(self, texts):
        return embed_for_similarity(list(texts), self.model_name, self.api_key)


def get_embedding_client() -> Optional[EmbeddingClient]:
    client = current_app.config.get('LEARNING_EMBEDDING_CLIENT')
    if client is not None:
        return client
    api_key = gemini_api_key()
    return GeminiEmbeddingClient(api_key) if api_key else None
