"""Gemini への接続設定のテスト（通信はしない）"""
import pytest
from langchain_core.messages import AIMessage

from app.learning import explanation as chain
from app.learning.llm import GeminiChatClient, LLMUnavailable, get_llm_client
from app.utils import gemini
from app.utils.langchain_analyzer import ReportGenerator
from tests.test_api_core import client  # noqa: F401  (fixture の再利用)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ('GEMINI_API_KEY', 'GOOGLE_API_KEY', 'GEMINI_MODEL', 'GEMINI_THINKING_LEVEL', 'GEMINI_EMBEDDING_MODEL',
                 'GOOGLE_GENAI_USE_VERTEXAI'):
        monkeypatch.delenv(name, raising=False)


def test_default_model_is_gemini_3_8_flash():
    assert gemini.gemini_model_name() == 'gemini-3.8-flash'


def test_api_key_prefers_gemini_api_key(monkeypatch):
    monkeypatch.setenv('GOOGLE_API_KEY', 'google-key')
    assert gemini.gemini_api_key() == 'google-key'
    monkeypatch.setenv('GEMINI_API_KEY', 'gemini-key')
    assert gemini.gemini_api_key() == 'gemini-key'


def test_gemini_3_does_not_receive_temperature():
    model = gemini.build_chat_model('gemini-3.8-flash', api_key='dummy', temperature=0.0)
    assert model.temperature is None
    older = gemini.build_chat_model('gemini-2.5-flash', api_key='dummy', temperature=0.2)
    assert older.temperature == pytest.approx(0.2)


def test_always_uses_developer_api_not_vertex(monkeypatch):
    # Firebase / Cloud Vision 用の Google Cloud 設定があっても Vertex AI に切り替わらない
    monkeypatch.setenv('GOOGLE_CLOUD_PROJECT', 'some-project')
    model = gemini.build_chat_model(api_key='dummy')
    assert model.vertexai is False


def test_thinking_level_is_passed_only_when_configured(monkeypatch):
    assert gemini.build_chat_model(api_key='dummy').thinking_level is None
    monkeypatch.setenv('GEMINI_THINKING_LEVEL', 'low')
    assert gemini.build_chat_model(api_key='dummy').thinking_level == 'low'


def test_message_text_handles_content_parts():
    assert gemini.message_text(AIMessage(content='本文')) == '本文'
    parts = AIMessage(content=[{'type': 'text', 'text': '前半'}, {'type': 'text', 'text': '後半'}])
    assert gemini.message_text(parts) == '前半後半'


def test_structured_output_sends_json_schema():
    client = GeminiChatClient('dummy')
    schema = chain._chain1_schema('quadratic_functions')
    structured = client._chat(0.0).with_structured_output(schema, method='json_schema')
    bound = structured.first
    assert bound.kwargs['response_mime_type'] == 'application/json'
    node_enum = bound.kwargs['response_json_schema']['properties']['steps']['items']['properties']['nodes']['items']['enum']
    assert 'double_root' in node_enum


def test_get_llm_client_uses_gemini_key(client, monkeypatch):
    with client.application.app_context():
        client.application.config['LEARNING_LLM_CLIENT'] = None
        assert get_llm_client() is None
        with pytest.raises(LLMUnavailable, match='GEMINI_API_KEY'):
            get_llm_client(required=True)
        monkeypatch.setenv('GEMINI_API_KEY', 'dummy')
        llm = get_llm_client()
        assert isinstance(llm, GeminiChatClient) and llm.model_name == 'gemini-3.8-flash'


def test_learning_api_reports_missing_gemini_key(client):
    from tests.test_api_core import _register_and_login
    _register_and_login(client, teacher_id='teacher_gemini', password='secret12')
    res = client.post('/api/learning/problems', json={
        'unit': 'quadratic_functions', 'title': '練習31', 'problem_text': 'x^2+mx-m+3=0 が重解をもつ m を求めよ。'})
    assert res.status_code == 503
    assert 'GEMINI_API_KEY' in res.get_json()['message']


# ---------------------------------------------------------------- 既存のレポート生成

class _FakeChat:
    def __init__(self, outcome):
        self.outcome = outcome

    def invoke(self, prompt):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return AIMessage(content=[{'type': 'text', 'text': self.outcome}])


def _report_data():
    return {'student_name': 'S001', 'subject': '数学I', 'test_name': 'No.1', 'score': 60,
            'correct_answers': 3, 'questions': 5, 'error_patterns': [{'pattern': '設問 2', 'count': 1}]}


def test_report_generator_requires_gemini_key():
    with pytest.raises(ValueError, match='GEMINI_API_KEY'):
        ReportGenerator()


def test_report_generator_falls_back_to_next_model_when_not_found(monkeypatch):
    monkeypatch.setenv('GEMINI_API_KEY', 'dummy')
    generator = ReportGenerator()
    assert generator.model_candidates[0] == 'gemini-3.8-flash'
    tried = []

    def build(model_name):
        tried.append(model_name)
        if model_name == 'gemini-3.8-flash':
            return _FakeChat(RuntimeError('404 NOT_FOUND: model is not found'))
        return _FakeChat('分析レポート本文')

    monkeypatch.setattr(generator, '_build_llm', build)
    assert generator.generate_analysis_report(_report_data()) == '分析レポート本文'
    assert tried[:2] == ['gemini-3.8-flash', 'gemini-3.6-flash']


def test_report_generator_uses_local_fallback_on_quota_error(monkeypatch):
    monkeypatch.setenv('GEMINI_API_KEY', 'dummy')
    generator = ReportGenerator()
    monkeypatch.setattr(generator, '_build_llm',
                        lambda name: _FakeChat(RuntimeError('429 RESOURCE_EXHAUSTED: quota exceeded')))
    report = generator.generate_analysis_report(_report_data())
    assert report.startswith('【ローカル生成レポート】')
    assert 'Gemini API がクォータ不足' in report


# ---------------------------------------------------------------- 埋め込み（意味の類似度）

class _FakeEmbeddings:
    def __init__(self):
        self.calls = []

    def embed_documents(self, texts, task_type=None):
        self.calls.append((list(texts), task_type))
        return [[1.0, 0.0] for _ in texts]


def test_default_embedding_model_and_backend():
    embeddings = gemini.build_embeddings(api_key='dummy')
    assert embeddings.model.endswith('gemini-embedding-2')
    assert embeddings.vertexai is False


def test_embedding_2_uses_similarity_instruction(monkeypatch):
    fake = _FakeEmbeddings()
    monkeypatch.setattr(gemini, 'build_embeddings', lambda name, key: fake)
    gemini.embed_for_similarity(['解説A'], 'gemini-embedding-2', 'dummy')
    assert fake.calls == [(['task: sentence similarity | query: 解説A'], None)]


def test_embedding_001_uses_task_type(monkeypatch):
    fake = _FakeEmbeddings()
    monkeypatch.setattr(gemini, 'build_embeddings', lambda name, key: fake)
    gemini.embed_for_similarity(['解説A'], 'gemini-embedding-001', 'dummy')
    assert fake.calls == [(['解説A'], 'SEMANTIC_SIMILARITY')]


def test_embedding_model_can_be_configured(monkeypatch):
    monkeypatch.setenv('GEMINI_EMBEDDING_MODEL', 'gemini-embedding-001')
    assert gemini.gemini_embedding_model_name() == 'gemini-embedding-001'


def test_thinking_level_per_call_overrides_environment(monkeypatch):
    monkeypatch.setenv('GEMINI_THINKING_LEVEL', 'high')
    assert gemini.build_chat_model(api_key='dummy', thinking_level='low').thinking_level == 'low'
    assert gemini.build_chat_model(api_key='dummy').thinking_level == 'high'


def test_chat_model_has_timeout_and_limited_retries(monkeypatch):
    from app.utils.gemini import build_chat_model

    monkeypatch.delenv('GEMINI_TIMEOUT', raising=False)
    model = build_chat_model('gemini-2.5-flash', api_key='dummy')
    assert model.timeout == 180 and model.max_retries == 2
    monkeypatch.setenv('GEMINI_TIMEOUT', '30')
    assert build_chat_model('gemini-2.5-flash', api_key='dummy').timeout == 30
