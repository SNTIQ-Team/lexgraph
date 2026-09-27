"""Binary document retrieval metrics, with identity checks before scoring.

Input: [{"qid": "q1", "relevant": ["document-id"], "ranking": [...]}].
IDs must already identify the benchmark's unit (e.g. a norm, not its chunks).
Duplicate IDs are errors: resolve chunk/document identity before evaluation.
Empty rankings are valid misses; empty gold sets are not evaluated as misses.
"""
import argparse
import json
from math import log2
from pathlib import Path


def _ids(value, field):
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError(f'{field} must be a list of nonempty canonical IDs')
    if len(set(value)) != len(value):
        raise ValueError(f'duplicate document identity in {field}')
    return value


def evaluate(rows, k=10):
    """Macro-average all questions in one run; reject ambiguous input."""
    if not isinstance(rows, list) or not rows:
        raise ValueError('a nonempty run is required')
    if type(k) is not int or k < 1:
        raise ValueError('k must be a positive integer')
    seen = set()
    results = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('each question must be an object')
        qid = row.get('qid')
        if not isinstance(qid, str) or not qid.strip() or qid in seen:
            raise ValueError('question identity must be nonempty and unique')
        seen.add(qid)
        gold = set(_ids(row.get('relevant'), 'relevant'))
        if not gold:
            raise ValueError('missing gold labels cannot be scored as a miss')
        ranking = _ids(row.get('ranking'), 'ranking')[:k]
        ranks = [i for i, doc in enumerate(ranking, 1) if doc in gold]
        ideal = sum(1 / log2(i + 1) for i in range(1, min(k, len(gold)) + 1))
        results.append({
            'qid': qid,
            'mrr': 1 / ranks[0] if ranks else 0.0,
            'ndcg': sum(1 / log2(i + 1) for i in ranks) / ideal,
            'recall': len(ranks) / len(gold),
            'precision': len(ranks) / k,
            'precision_at_1': float(bool(ranks) and ranks[0] == 1),
        })
    aggregate = {name: sum(row[name] for row in results) / len(results)
                 for name in results[0] if name != 'qid'}
    return {'num_queries': len(results), 'k': k, 'aggregate': aggregate,
            'queries': results, 'relevance': 'binary', 'averaging': 'macro',
            'identity_policy': 'unique_canonical_documents'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=Path)
    parser.add_argument('--k', type=int, default=10)
    args = parser.parse_args()
    print(json.dumps(evaluate(json.loads(args.run.read_text()), args.k), indent=2))


if __name__ == '__main__':
    main()
