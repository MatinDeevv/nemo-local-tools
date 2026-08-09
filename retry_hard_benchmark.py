"""Retry only failed entries from nvidia_hard_benchmark_results.json at low concurrency."""
import concurrent.futures
import json
import os
from pathlib import Path
import nvidia_hard_benchmark as bench

SOURCE=Path('nvidia_hard_benchmark_results.json')

def retry_key(key_number, entries):
    api_key=os.environ[f'NVIDIA_API_KEY_{key_number}']
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futures=[pool.submit(bench.ask,key_number,api_key,e['question'],bench.QUESTIONS[e['question']-1]) for e in entries]
        return [future.result() for future in concurrent.futures.as_completed(futures)]

def main():
    bench.load_env(r'C:\Users\marti\Desktop\.env')
    data=json.loads(SOURCE.read_text(encoding='utf-8'))
    # Model 2 was unavailable on the hosted endpoint; retry it with GLM-5.2.
    data['models']={str(k):v for k,v in bench.MODELS.items()}
    failed={}
    for r in data['results']:
        if not r['ok']: failed.setdefault(r['key'],[]).append(r)
    replacements=[]
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        futures=[pool.submit(retry_key,k,rows) for k,rows in failed.items()]
        for future in concurrent.futures.as_completed(futures): replacements.extend(future.result())
    lookup={(r['key'],r['question']):r for r in replacements}
    data['results']=[lookup.get((r['key'],r['question']),r) for r in data['results']]
    data['results'].sort(key=lambda r:(r['key'],r['question']))
    SOURCE.write_text(json.dumps(data,indent=2),encoding='utf-8')
    print('Completed',sum(r['ok'] for r in data['results']),'/',len(data['results']))
    for k in sorted(bench.MODELS): print(k,bench.MODELS[k],sum(r['ok'] for r in data['results'] if r['key']==k),'/40')

if __name__=='__main__': main()
