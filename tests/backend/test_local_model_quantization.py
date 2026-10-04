import json
import sys
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from tavern.core.models import Settings
from tavern.core.settings import SettingsDocument
from tavern.search.local_models import LocalModelManager, _worker
from tavern.search.retrieval import digest


def test_quantization_roundtrip_reuses_weights_but_requires_new_worker(tmp_path):
    document = SettingsDocument()
    document.shared.local_models.embedding.quantization = 'int8'
    restored = SettingsDocument.model_validate_json(document.model_dump_json())
    assert restored.runtime().local_models.embedding.quantization == 'int8'
    with pytest.raises(ValidationError):
        Settings(local_models={'embedding': {'model': 'Qwen/Qwen3-Embedding-4B', 'quantization': 'fp4'}})

    settings = Settings()
    config = settings.local_models.embedding
    manager = LocalModelManager(SimpleNamespace(root=tmp_path))
    path = manager.path(config)
    path.mkdir(parents=True)
    (path / 'tavern-ready.json').write_text(json.dumps({'revision': 'cached'}), encoding='utf-8')
    manager.workers['embedding'] = (SimpleNamespace(is_alive=lambda: True), None, digest(config.model_dump()))
    manager.ready.add('embedding')
    assert manager.status(settings)['models']['embedding']['ready']
    config.quantization = 'int8'
    assert manager.path(config) == path
    status = manager.status(settings)['models']['embedding']
    assert status['downloaded'] and not status['ready']


def test_int8_worker_stays_on_selected_gpu_and_batches_rerank_fragments(monkeypatch, tmp_path):
    torch = pytest.importorskip('torch')
    monkeypatch.setenv('HF_HUB_OFFLINE', '1')
    monkeypatch.setenv('TRANSFORMERS_OFFLINE', '1')
    for name, function in {
        'is_available': lambda: True,
        'mem_get_info': lambda device: (6 * 2**30, 12 * 2**30),
        'get_device_name': lambda device: 'fixture GPU',
        'memory_allocated': lambda device: 1024,
        'memory_reserved': lambda device: 2048,
        'max_memory_allocated': lambda device: 4096,
        'max_memory_reserved': lambda device: 8192,
        'reset_peak_memory_stats': lambda device: None,
    }.items():
        monkeypatch.setattr(torch.cuda, name, function)
    monkeypatch.setattr('tavern.search.local_models.find_spec', lambda name: object())
    loaded, forwarded, batches = [], [], []

    class Inputs(dict):
        def to(self, device):
            assert device == 'cuda:1'
            return self

    class Tokenizer:
        def convert_tokens_to_ids(self, token):
            return {'no': 0, 'yes': 1}[token]

        def __call__(self, texts, **kwargs):
            batches.append(texts)
            return Inputs(values=torch.tensor([[0., 2.] if 'positive' in t else [2., 0.] for t in texts]))

    class Model:
        def eval(self):
            return self

        def to(self, device):
            raise AssertionError('INT8 must load directly on the selected GPU')

        def __call__(self, values, **kwargs):
            forwarded.append(kwargs)
            return SimpleNamespace(logits=values[:, None, :])

    def load(path, **kwargs):
        loaded.append(kwargs)
        return Model()

    factory = SimpleNamespace(from_pretrained=load)
    monkeypatch.setitem(sys.modules, 'transformers', SimpleNamespace(
        AutoModel=factory, AutoModelForCausalLM=factory,
        AutoTokenizer=SimpleNamespace(from_pretrained=lambda *args, **kwargs: Tokenizer()),
        BitsAndBytesConfig=lambda **kwargs: kwargs,
    ))
    messages = []
    request = {'action': 'rerank', 'data': [['question', ['negative', 'positive', 'negative']]],
               'instruction_prompt': 'retrieve', 'reranker_prompt': 'judge', 'rerank_fragments': 3}
    requests = iter([request])

    def receive():
        try:
            return next(requests)
        except StopIteration:
            raise EOFError from None

    pipe = SimpleNamespace(recv=receive, send=messages.append, close=lambda: None)
    config = {'model': 'Qwen/Qwen3-Reranker-4B', 'revision': 'main', 'device': 'cuda:1',
              'quantization': 'int8', 'batch_size': 1}
    _worker(pipe, tmp_path, 'reranker', config)
    assert not any('error' in message for message in messages), messages
    assert loaded[0]['device_map'] == {'': 'cuda:1'}
    assert loaded[0]['local_files_only'] is True
    assert loaded[0]['quantization_config']['llm_int8_enable_fp32_cpu_offload'] is False
    assert loaded[0]['quantization_config']['llm_int8_threshold'] == 6.0
    assert loaded[0]['quantization_config']['llm_int8_skip_modules'] == ['lm_head']
    assert list(map(len, batches)) == [1, 1, 1]
    assert all(call == {'use_cache': False, 'logits_to_keep': 1} for call in forwarded)
    assert messages[-1]['result'] == pytest.approx([0.880797], abs=1e-6)
