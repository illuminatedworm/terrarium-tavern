"""Compare local retrieval models on a private card with pre-labeled queries.

Reads the application database without changing it. Uses the application's real
CUDA workers and retrieval engine, but writes indexes/results to the output folder.
Models must already be downloaded in the application's data/models directory.
"""
import argparse
import asyncio
import json
import math
import random
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

from tavern.character.cards import normalize
from tavern.core.settings import SettingsDocument
from tavern.search.local_models import LocalModelManager
from tavern.search.retrieval import RetrievalEngine, digest, lexical_recall, tokenizers
from tavern.storage.store import Store


class GpuMonitor:
    def __init__(self):
        self.samples = []
        self.process = None
        self.thread = None

    def start(self):
        self.process = subprocess.Popen(
            ['nvidia-smi', '--query-gpu=memory.used', '--format=csv,noheader,nounits', '-i', '0', '-lms', '200'],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )

        def read():
            for line in self.process.stdout:
                try:
                    self.samples.append((time.perf_counter(), float(line.strip())))
                except ValueError:
                    continue

        self.thread = threading.Thread(target=read, daemon=True)
        self.thread.start()

    def peak(self, start, end):
        values = [value for timestamp, value in self.samples if start <= timestamp <= end]
        return max(values) if values else None

    def close(self):
        if self.process:
            self.process.terminate()
            self.process.wait(timeout=10)
            self.thread.join(timeout=2)
            self.process.stdout.close()


class MeasuredModels(LocalModelManager):
    def __init__(self, store):
        super().__init__(store)
        self.calls = []

    async def infer(self, purpose, action, data, settings, **kwargs):
        started = time.perf_counter()
        value = await super().infer(purpose, action, data, settings, **kwargs)
        self.calls.append({'purpose': purpose, 'action': action,
                           'ms': (time.perf_counter() - started) * 1000,
                           'metrics': dict(self.metrics[purpose])})
        return value


def percentile(values, fraction):
    values = sorted(values)
    position = (len(values) - 1) * fraction
    low, high = math.floor(position), math.ceil(position)
    return values[low] + (values[high] - values[low]) * (position - low)


def quality(ids, gold):
    ids = list(map(str, ids))
    positions = [i + 1 for i, eid in enumerate(ids) if eid in gold]
    return {f'hit{k}': int(bool(positions) and min(positions) <= k) for k in (1, 5, 10)} | {
        'mrr10': 1 / min(positions) if positions and min(positions) <= 10 else 0,
        'returned': len(ids),
    }


def summarize(rows, default_budgets):
    positive = [r for r in rows if r['relevant']]
    negative = [r for r in rows if not r['relevant']]
    first = [r for r in positive if r['repeat'] == 0]
    summary = {key: statistics.mean(r[key] for r in first) for key in ('hit1', 'hit5', 'hit10', 'mrr10')}
    dense = [quality(r['rankings'].get('embedding', []), set(r['relevant'])) for r in first]
    summary['embedding_before_fusion'] = {
        key: statistics.mean(r[key] for r in dense) for key in ('hit1', 'hit5', 'hit10', 'mrr10')
    }
    for kind in ('named', 'paraphrase'):
        group = [r for r in first if r['kind'] == kind]
        summary[kind] = {key: statistics.mean(r[key] for r in group) for key in ('hit1', 'hit5', 'mrr10')} if group else {}
    summary['negative_empty_rate'] = statistics.mean(not r['ids'] for r in negative) if negative else None
    summary['degraded_requests'] = sum(bool(r['degraded']) for r in rows)
    for group_name, group in [('all', rows), ('positive', positive)]:
        summary[group_name + '_latency_ms'] = {
            'p50': statistics.median(r['ms'] for r in group),
            'p95': percentile([r['ms'] for r in group], .95),
        }
    summary['stage_latency_ms'] = {}
    for stage in ('embedding_query', 'reranker'):
        values = [r['channel_ms'][stage] for r in positive if stage in r['channel_ms']]
        if values:
            summary['stage_latency_ms'][stage] = {'p50': statistics.median(values), 'p95': percentile(values, .95)}
    summary['default_budget_exceedances_estimated'] = {
        'embedding': sum(r['channel_ms'].get('embedding_query', 0) > default_budgets['embedding'] * 1000 for r in rows),
        'reranker': sum(r['channel_ms'].get('reranker', 0) > default_budgets['reranker'] * 1000 for r in rows),
        'requests': len(rows),
    }
    return summary


async def main(args):
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect((args.data / 'tavern.sqlite3').resolve().as_uri() + '?mode=ro', uri=True) as source:
        cards = source.execute('SELECT id,document FROM cards WHERE name=?', (args.card_name,)).fetchall()
        if len(cards) != 1:
            raise ValueError('Card name must identify exactly one card')
        card_id, raw_card = cards[0]
        document = json.loads(raw_card)
        settings = SettingsDocument.model_validate(json.loads(source.execute(
            "SELECT value FROM meta WHERE key='settings'").fetchone()[0])).runtime(args.mode)
    fixture = json.loads(args.queries.read_text('utf-8'))
    cases = fixture['queries']
    default_budgets = {'embedding': settings.retrieval.embedding_timeout, 'reranker': settings.retrieval.rerank_timeout}
    settings.retrieval.channels = ['keyword', 'bm25', 'embedding']
    settings.local_models.reranker_enabled = True
    # Measure successful retrieval rather than censoring slower models at the production timeout.
    settings.retrieval.embedding_timeout = 120
    settings.retrieval.rerank_timeout = 120
    size, quantization = ('0.6B', 'none') if args.variant == '0.6b-bf16' else ('4B', 'int8')
    for purpose, kind in [('embedding', 'Embedding'), ('reranker', 'Reranker')]:
        config = getattr(settings.local_models, purpose)
        config.model = f'Qwen/Qwen3-{kind}-{size}'
        config.quantization = quantization
        config.batch_size = args.batch_size
        config.dimensions = args.dimensions if purpose == 'embedding' else 1024
    store = Store(args.output.parent / f'store-{args.variant}-{args.dimensions}d')
    session = {'id': 'benchmark', 'persona': '用户', 'agent_mode': args.mode, 'lore_injection_mode': 'conversation'}
    with store.connection() as db:
        db.execute('INSERT OR IGNORE INTO sessions(id,card,persona,recipe,created,card_data,lore_injection_mode) '
                   'VALUES(?,?,?,?,?,?,?)', (session['id'], card_id, session['persona'], '{}', time.time(), '{}', 'conversation'))
    models = MeasuredModels(store)
    models.root = (args.data / 'models').resolve()
    engine = RetrievalEngine(store, models)
    monitor = GpuMonitor()
    rows = []
    report = {'card': args.card_name, 'card_digest': digest(document), 'fixture_digest': digest(fixture),
              'variant': args.variant, 'model_root': str(models.root), 'batch_size': args.batch_size,
              'dimensions': args.dimensions, 'repeat': args.repeat, 'default_budgets_seconds': default_budgets,
              'measurement_timeout_seconds': 120, 'warmup': {}, 'results': rows,
              'started_at_utc': datetime.now(timezone.utc).isoformat(), 'python': sys.executable,
              'software': {name: version(name) for name in ('torch', 'transformers', 'bitsandbytes', 'accelerate')},
              'retrieval_settings': settings.retrieval.model_dump(),
              'local_model_settings': settings.local_models.model_dump(),
              'empty_history_and_timeline': True, 'final_context_compilation_included': False,
              'fixture_description': fixture.get('description', '')}
    started = time.perf_counter()
    try:
        tokenizers()
        entries = lexical_recall(document, [], '', {}, settings, session, 'corpus')['entries']
        report['card_entries'] = len(normalize(document)['character_book']['entries'])
        report['retrievable_entries'] = len(entries)
        entry_ids = {str(e['id']) for e in entries}
        for case in cases:
            if not set(case['relevant']) <= entry_ids:
                raise ValueError(f"Gold IDs not in retrievable corpus: {case['id']}")
        monitor.start()
        await asyncio.sleep(.4)
        report['idle_gpu_mib'] = monitor.peak(started, time.perf_counter())
        for purpose in ('embedding', 'reranker'):
            before = time.perf_counter()
            await models.warmup(purpose, settings)
            manifest = json.loads((models.path(getattr(settings.local_models, purpose)) / 'tavern-ready.json').read_text('utf-8'))
            report['warmup'][purpose] = {'seconds': time.perf_counter() - before,
                                         'revision': manifest['revision'], **models.metrics[purpose]}
            print(json.dumps({'phase': 'warmup', 'purpose': purpose, **report['warmup'][purpose]}), flush=True)
        report['both_models_resident'] = True
        index_start = time.perf_counter()
        await engine.prepare(document, [], '', {}, settings, session, 'index', index_only=True)
        pending = [task for task in models.tasks.values() if not task.done()]
        while pending:
            _, pending = await asyncio.wait(pending, timeout=15)
            print(json.dumps({'phase': 'index', 'jobs': list(models.jobs.values())}, ensure_ascii=False), flush=True)
        failed = [job for job in models.jobs.values() if job['status'] != 'complete']
        if failed:
            raise RuntimeError(failed)
        report['index_seconds'] = time.perf_counter() - index_start
        report['index_gpu_peak_mib'] = monitor.peak(index_start, time.perf_counter())
        with store.connection() as db:
            cached = [json.loads(r[0]) for r in db.execute('SELECT value FROM retrieval_vectors')]
        report['indexed_fragments'] = sum(len(value['fragments']) for value in cached)
        warm = await engine.prepare(document, [], cases[0]['text'], {}, settings, session, 'untimed-warmup')
        report['untimed_warmup_diagnostics'] = warm['diagnostics']
        query_start = time.perf_counter()
        for repeat in range(args.repeat):
            order = list(cases)
            random.Random(20261004 + repeat).shuffle(order)
            for case in order:
                before = time.perf_counter()
                result = await engine.prepare(document, [], case['text'], {}, settings, session,
                                              f"measured-{repeat}-{case['id']}")
                row = {**case, 'repeat': repeat, 'ids': result['ids'], 'ms': (time.perf_counter() - before) * 1000,
                       'channel_ms': result['channel_ms'], 'rankings': result.get('rankings', {}),
                       'rerank_scores': result.get('rerank_scores', {}),
                       'degraded': [d for d in result['diagnostics'] if '降级' in str(d) or '不可用' in str(d)],
                       **quality(result['ids'], set(case['relevant']))}
                rows.append(row)
                print(json.dumps({'phase': 'query', 'id': case['id'], 'repeat': repeat,
                                  'ms': round(row['ms'], 1), 'hit5': row['hit5'], 'ids': row['ids'][:5],
                                  'degraded': row['degraded']}, ensure_ascii=False), flush=True)
                args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        await asyncio.sleep(.3)
        report['query_gpu_peak_mib'] = monitor.peak(query_start, time.perf_counter())
        report['summary'] = summarize(rows, default_budgets)
        report['model_peak_allocated_mib'] = {purpose: max(
            [call['metrics'].get('peak_allocated_bytes', 0) / 2**20 for call in models.calls if call['purpose'] == purpose],
            default=0) for purpose in ('embedding', 'reranker')}
        report['status'] = 'complete'
    except Exception as error:
        report['status'] = 'error'
        report['error'] = repr(error)
        raise
    finally:
        report['elapsed_seconds'] = time.perf_counter() - started
        report['gpu_peak_including_loading_mib'] = monitor.peak(started, time.perf_counter())
        report['model_calls'] = models.calls
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        await models.close()
        monitor.close()
    print(json.dumps(report['summary'], ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, default=Path('data'))
    parser.add_argument('--card-name', required=True)
    parser.add_argument('--queries', type=Path, required=True)
    parser.add_argument('--variant', choices=['0.6b-bf16', '4b-int8'], required=True)
    parser.add_argument('--batch-size', type=int, default=1)
    parser.add_argument('--dimensions', type=int, default=1024, help='Embedding output dimensions; reranker is unchanged')
    parser.add_argument('--repeat', type=int, default=2)
    parser.add_argument('--mode', choices=['half_agent', 'full_agent'], default='half_agent')
    parser.add_argument('--output', type=Path, required=True)
    arguments = parser.parse_args()
    if arguments.repeat < 1 or not 1 <= arguments.batch_size <= 128:
        parser.error('repeat must be positive and batch-size must be between 1 and 128')
    maximum_dimensions = 1024 if arguments.variant == '0.6b-bf16' else 2560
    if not 32 <= arguments.dimensions <= maximum_dimensions:
        parser.error(f'dimensions must be between 32 and {maximum_dimensions} for {arguments.variant}')
    asyncio.run(main(arguments))
