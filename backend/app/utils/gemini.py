"""Gemini（Google Gen AI）の共通設定

環境変数:
  GEMINI_API_KEY        Google AI Studio で発行した API キー（GOOGLE_API_KEY も予備として受け付ける）
  GEMINI_MODEL          モデル名（既定: gemini-3.1-pro-preview）
  GEMINI_THINKING_LEVEL 任意。思考の深さ（low / high など）。未設定ならモデルの既定
  GEMINI_EMBEDDING_MODEL 埋め込みモデル（既定: gemini-embedding-2）。解説どうしの意味の類似度（5.5節）に使う

Firebase・Cloud Vision 用の GOOGLE_APPLICATION_CREDENTIALS と混線しないよう，常に API キー方式
（Gemini Developer API）で接続し，Vertex AI には切り替えない。
"""
from __future__ import annotations

import os
import re
from typing import Any, Optional

DEFAULT_MODEL = 'gemini-3.1-pro-preview'
DEFAULT_EMBEDDING_MODEL = 'gemini-embedding-2'
# gemini-embedding-2 は用途を引数ではなく本文の前の指示で与える（意味的類似度の指示）
SIMILARITY_PREFIX = 'task: sentence similarity | query: '


def gemini_api_key() -> str:
    return (os.getenv('GEMINI_API_KEY', '').strip() or os.getenv('GOOGLE_API_KEY', '').strip())


def gemini_model_name() -> str:
    return os.getenv('GEMINI_MODEL', '').strip() or DEFAULT_MODEL


def is_gemini_3_or_later(model_name: str) -> bool:
    match = re.match(r'gemini-(\d+)', model_name)
    return bool(match) and int(match.group(1)) >= 3


def build_chat_model(model_name: Optional[str] = None, api_key: Optional[str] = None,
                     temperature: Optional[float] = None, **kwargs: Any):
    """ChatGoogleGenerativeAI を作る。

    Gemini 3 以降では temperature を渡さない（Google の推奨。明示すると推論の劣化やループの原因になる）。
    """
    from langchain_google_genai import ChatGoogleGenerativeAI

    name = model_name or gemini_model_name()
    params: dict[str, Any] = {'model': name, 'api_key': api_key or gemini_api_key(), 'vertexai': False}
    if temperature is not None and not is_gemini_3_or_later(name):
        params['temperature'] = temperature
    thinking_level = os.getenv('GEMINI_THINKING_LEVEL', '').strip()
    if thinking_level and is_gemini_3_or_later(name):
        params['thinking_level'] = thinking_level
    params.update(kwargs)
    return ChatGoogleGenerativeAI(**params)


def message_text(message: Any) -> str:
    """AIMessage から本文の文字列を取り出す（Gemini 3 は content が部品のリストになることがある）。"""
    text = getattr(message, 'text', None)
    if isinstance(text, str):  # langchain-core 1.x では文字列（呼び出しも可能だが非推奨）
        return str(text)
    if callable(text):  # 古い langchain-core ではメソッド
        text = text()
        if isinstance(text, str):
            return text
    content = getattr(message, 'content', message)
    if isinstance(content, list):
        return ''.join(part.get('text', '') if isinstance(part, dict) else str(part) for part in content)
    return str(content)


def gemini_embedding_model_name() -> str:
    return os.getenv('GEMINI_EMBEDDING_MODEL', '').strip() or DEFAULT_EMBEDDING_MODEL


def build_embeddings(model_name: Optional[str] = None, api_key: Optional[str] = None):
    from langchain_google_genai import GoogleGenerativeAIEmbeddings

    return GoogleGenerativeAIEmbeddings(model=model_name or gemini_embedding_model_name(),
                                        google_api_key=api_key or gemini_api_key(), vertexai=False)


def embed_for_similarity(texts: list[str], model_name: Optional[str] = None,
                         api_key: Optional[str] = None) -> list[list[float]]:
    """文章どうしの意味の類似度を測るための埋め込みを返す。

    gemini-embedding-001 には task_type=SEMANTIC_SIMILARITY を指定し，それ以降のモデル
    （gemini-embedding-2 など）には意味的類似度の指示を本文の前に付ける。
    """
    name = model_name or gemini_embedding_model_name()
    embeddings = build_embeddings(name, api_key)
    if name.endswith('embedding-001'):
        return embeddings.embed_documents(texts, task_type='SEMANTIC_SIMILARITY')
    return embeddings.embed_documents([SIMILARITY_PREFIX + t for t in texts])
