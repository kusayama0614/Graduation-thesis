"""LLM 呼び出しの抽象化

本システムで LLM が担うのは，(a) 構造化された出力（Chain 1 の分解，誤りの診断，計算の抜き出し）と
(b) 決められた方針に沿った文章の作成（Chain 2）だけである。正誤や詳しさの決定は LLM に任せない。

LLM には Gemini（既定 gemini-3.8-flash）を用いる。設定は app/utils/gemini.py を参照。
テストや API キーのない環境では，Flask の設定 `LEARNING_LLM_CLIENT` に同じインターフェースを持つ
オブジェクトを入れて差し替える。
"""
from __future__ import annotations

import os
import re
import time
from typing import Any, Callable, Dict, List, Optional, Protocol

from flask import current_app

from app.utils.diagnostics import log, timed
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


# Gemini 側の一時的な混雑・時間切れ（503 UNAVAILABLE「high demand」，504 DEADLINE_EXCEEDED，500 INTERNAL）。
# 少し待てば通ることが多いので，間をあけて作り直す（利用上限の 429 は待っても通らないので対象外）
TRANSIENT_PATTERN = re.compile(r'\b50[034]\b|UNAVAILABLE|DEADLINE_EXCEEDED|high demand|overloaded|INTERNAL', re.IGNORECASE)


def transient_retry_delays() -> List[float]:
    """一時的なエラーのあとに待つ秒数（GEMINI_TRANSIENT_RETRY_DELAYS，既定「15,45,90」）。"""
    raw = os.getenv('GEMINI_TRANSIENT_RETRY_DELAYS', '15,45,90')
    return [float(x) for x in raw.split(',') if x.strip()]


def call_with_retry(call: Callable[[], Any], what: str, delays: Optional[List[float]] = None,
                    sleep: Callable[[float], None] = time.sleep) -> Any:
    """call() を実行し，一時的なエラーなら delays の秒数ずつ待って作り直す。それ以外のエラーはそのまま送出する。"""
    delays = transient_retry_delays() if delays is None else delays
    for attempt, delay in enumerate([*delays, None]):
        try:
            return call()
        except Exception as exc:
            if delay is None or not TRANSIENT_PATTERN.search(str(exc)):
                raise
            log(f'{what}: Gemini が混雑しています（{str(exc)[:80]}）。{delay:g} 秒待って作り直します（{attempt + 1}/{len(delays)}）')
            sleep(delay)


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

        def call():
            with timed(f'Gemini {schema_name}', track=True) as info:
                info['detail'] = f'思考 {thinking_level or "既定"}'
                return structured.invoke([('system', system), ('human', user)])
        return call_with_retry(call, f'Gemini {schema_name}')

    def generate_text(self, *, system, user, temperature=0.3, thinking_level=None):
        chat = self._chat(temperature, thinking_level)

        def call():
            with timed('Gemini 文章', track=True) as info:
                message = chat.invoke([('system', system), ('human', user)])
                info['detail'] = f'思考 {thinking_level or "既定"}，' + _usage(message)
                return message_text(message)
        return call_with_retry(call, 'Gemini 文章')

    def read_image_json(self, *, system, user, image_bytes, mime_type, schema, schema_name, thinking_level=None):
        """画像（スキャンしたページなど）を読み取り，JSON Schema に従った出力を dict で返す。"""
        return self.read_images_json(system=system, user=user, images=[(image_bytes, mime_type)], schema=schema,
                                     schema_name=schema_name, thinking_level=thinking_level)

    def read_images_json(self, *, system, user, images, schema, schema_name, thinking_level=None):
        """複数の画像（答案の表面と裏面など）をまとめて読み取る。images は [(bytes, mime_type)]。"""
        import base64
        from langchain_core.messages import HumanMessage, SystemMessage

        content = [{'type': 'text', 'text': user}]
        for image_bytes, mime_type in images:
            data_url = f'data:{mime_type};base64,{base64.b64encode(image_bytes).decode("ascii")}'
            content.append({'type': 'image_url', 'image_url': {'url': data_url}})
        structured = self._chat(0.0, thinking_level).with_structured_output(schema, method='json_schema')
        return structured.invoke([SystemMessage(content=system), HumanMessage(content=content)])


def _usage(message) -> str:
    """応答のトークン数（入力・出力・うち思考）。遅い原因が思考の長さか，出力の長さかを見分けるため。"""
    usage = getattr(message, 'usage_metadata', None) or {}
    details = usage.get('output_token_details') or {}
    return (f"入力 {usage.get('input_tokens', '?')}・出力 {usage.get('output_tokens', '?')}"
            f"（うち思考 {details.get('reasoning', 0)}）トークン")


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
