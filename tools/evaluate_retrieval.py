"""Reproducible multilingual fixture evaluation, not a claim about arbitrary user cards."""
import argparse
import asyncio
import json
import math
import statistics
import tempfile
import time
from pathlib import Path

from tavern.conversation.context import activate_lore, compile_context
from tavern.core.models import ContextRecipe, Settings
from tavern.search.local_models import LocalModelManager
from tavern.search.retrieval import RetrievalEngine, sparse_index, tokenizers
from tavern.storage.store import Store


def metrics(predictions, gold):
    hit = [eid in gold for eid in predictions]
    dcg = sum(int(ok) / math.log2(i + 2) for i, ok in enumerate(hit[:5]))
    ideal = sum(1 / math.log2(i + 2) for i in range(min(5, len(gold))))
    return {'recall20': sum(hit[:20]) / max(1, len(gold)), 'ndcg5': dcg / max(1, ideal),
            'false_injection_rate': sum(not ok for ok in hit[:5]) / max(1, len(hit[:5]))}


async def main(neural, model_dir=None):
    fixture = json.loads(Path('tests/fixtures/retrieval_multilingual.json').read_text('utf-8'))
    doc = {'character_book':{'entries':fixture['entries']}}
    temporary = tempfile.TemporaryDirectory(prefix='retrieval-evaluation-')
    store = Store(Path(temporary.name))
    models = LocalModelManager(store)
    if model_dir:
        models.root = Path(model_dir).resolve()
    engine = RetrievalEngine(store, models)
    settings = Settings(lore_recursive=True)
    tokenizers()
    fixture['queries'].extend([{'text': text, 'relevant': []} for text in ['你好，谢谢。', '明天也要努力。', '今天都在休息。', '白色和银色都很好看。']])
    report = {'fixture':fixture['description'], 'query_count':len(fixture['queries']), 'modes':{},
              'positive_query_count': sum(bool(case['relevant']) for case in fixture['queries']),
              'negative_query_count': sum(not case['relevant'] for case in fixture['queries'])}
    try:
        modes = ['keyword_only','keyword_bm25']
        if neural:
            for purpose in ('embedding','reranker'):
                await models.warmup(purpose, settings)
            modes.extend(['hybrid','hybrid_rerank'])
            settings.retrieval.channels = ['keyword','bm25','embedding']
            await engine.prepare(doc, [], '', {}, settings, {'id':'eval'}, 'build')
            await asyncio.gather(*list(models.tasks.values()))
        for mode in modes:
            samples, times, degraded = [], [], 0
            settings.retrieval.channels = ['keyword','bm25','embedding'] if mode.startswith('hybrid') else ['keyword','bm25']
            settings.local_models.reranker_enabled = mode == 'hybrid_rerank'
            for i, case in enumerate(fixture['queries']):
                start = time.perf_counter()
                if mode == 'keyword_only':
                    entries, _, _ = activate_lore(doc, [], case['text'], {}, str(i), recursive=False,
                                                  timeline=case.get('timeline',''))
                    ids = [entry['id'] for entry in entries]
                else:
                    result = await engine.prepare(doc, [], case['text'], {'state':{'世界':{
                        '当前时间线':case.get('timeline','')}}}, settings, {'id':'eval'}, str(i))
                    ids = result['ids']
                    degraded += int(any('降级' in str(d) or '不可用' in str(d) for d in result['diagnostics']))
                session = {'id': 'eval', 'persona': '用户', 'recipe': ContextRecipe().model_dump(), 'lore_injection_mode': 'conversation'}
                prepared = {'ids': ids, 'query': case['text'], 'diagnostics': []} if mode == 'keyword_only' else result
                compiled = compile_context(doc, session, [], {'state': {'世界': {'当前时间线': case.get('timeline', '')}}},
                                           case['text'], settings, str(i), retrieval=prepared)
                ids = list(dict.fromkeys(eid for row in compiled['trace'] for eid in row.get('lore_ids', [])))
                times.append((time.perf_counter()-start)*1000)
                samples.append({**metrics(ids, case['relevant']), 'estimated_input_tokens': compiled['estimated_tokens']})
            report['modes'][mode] = {key:statistics.mean(row[key] for row in samples) for key in samples[0]}
            report['modes'][mode]['positive_recall20'] = statistics.mean(
                row['recall20'] for row, case in zip(samples, fixture['queries'], strict=True) if case['relevant'])
            report['modes'][mode].update(p50_ms=statistics.median(times), p95_ms=sorted(times)[int(.95*(len(times)-1))],
                                         degraded_queries=degraded)
        if neural:
            cross = []
            settings.retrieval.channels = ['embedding']
            settings.local_models.reranker_enabled = True
            for i, case in enumerate(fixture.get('cross_language_queries', [])):
                language_index = {'zh':0,'en':1,'ja':2}[case['document_language']]
                cross_doc = {'character_book':{'entries':[{'id':e['id'],'keys':[], 'content':e['content'].split('\n')[language_index]}
                                                          for e in fixture['entries']]}}
                await engine.prepare(cross_doc, [], '', {}, settings, {'id':'cross'}, f'build-{i}')
                await asyncio.gather(*[task for task in models.tasks.values() if not task.done()])
                result = await engine.prepare(cross_doc, [], case['text'], {}, settings, {'id':'cross'}, str(i))
                cross.append({'language':case['language'], 'document_language':case['document_language'],
                              'relevant':case['relevant'], 'retrieved':result['ids'], **metrics(result['ids'],case['relevant'])})
            report['cross_language'] = cross
        settings.retrieval.channels = ['keyword','bm25']
        settings.local_models.reranker_enabled = False
        entries = [{'id':str(i),'keys':[f'地点{i}'],'content':f'地点{i}的守卫在码头等待。'+'当地居民在这里生活，整理船只和工具。'*26}
                   for i in range(1000)]
        started = time.perf_counter()
        sparse_index(entries, settings.retrieval)
        cold = (time.perf_counter()-started)*1000
        times=[]
        for i in range(50):
            start=time.perf_counter()
            await engine.prepare({'character_book':{'entries':entries}},[],f'地点{i} 守卫',{},settings,{'id':'benchmark'},f'benchmark-{i}')
            times.append((time.perf_counter()-start)*1000)
        report['sparse_1000_entries']={'build_ms':cold,'p50_ms':statistics.median(times),
                                      'p95_ms':sorted(times)[int(.95*(len(times)-1))]}
        report['gpu_models'] = models.metrics
        baseline, hybrid = report['modes']['keyword_only'], report['modes']['keyword_bm25']
        report['acceptance'] = {
            'fixture_quality_pass': hybrid['recall20'] > baseline['recall20'] and hybrid['false_injection_rate'] <= baseline['false_injection_rate'],
            'warm_sparse_p95_under_100ms': report['sparse_1000_entries']['p95_ms'] < 100,
        }
        output = Path('.cache/retrieval-validation/retrieval-evaluation.json')
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(report,ensure_ascii=False,indent=2))
        if not all(report['acceptance'].values()):
            raise RuntimeError('检索验收指标未达标，请查看评估报告')
    finally:
        await models.close()
        temporary.cleanup()


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--neural',action='store_true')
    parser.add_argument('--model-dir', help='Read already-downloaded weights from this directory; never downloads')
    args = parser.parse_args()
    asyncio.run(main(args.neural, args.model_dir))
