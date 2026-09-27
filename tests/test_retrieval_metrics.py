import pytest
from tools.retrieval_metrics import evaluate


def row(ranking, relevant=None, qid='q'):
    return {'qid': qid, 'ranking': ranking, 'relevant': relevant or ['gold']}


@pytest.mark.parametrize('ranking', [
    ['gold', 'gold'], ['miss', 'miss', 'gold'], ['gold'] + ['x'] * 10,
])
def test_repeated_identity_never_inflates_scores(ranking):
    with pytest.raises(ValueError, match='duplicate document identity'):
        evaluate([row(ranking)])


def test_empty_retrieval_is_included_in_denominator():
    result = evaluate([row(['gold']), row([], qid='miss')])
    assert result['num_queries'] == 2
    assert result['aggregate'] == {
        'mrr': .5, 'ndcg': .5, 'recall': .5,
        'precision': .05, 'precision_at_1': .5,
    }


def test_multi_gold_uses_ideal_dcg_and_unique_recall():
    result = evaluate([row(['b', 'a'], ['a', 'b'])], k=2)
    assert result['aggregate']['ndcg'] == 1
    assert result['aggregate']['recall'] == 1


def test_rank_and_top_k_are_preserved():
    result = evaluate([row(['a', 'b', 'gold'])], k=2)
    assert all(v == 0 for v in result['aggregate'].values())
    result = evaluate([row(['a', 'b', 'gold'])], k=3)
    assert result['aggregate']['ndcg'] == .5
    assert result['aggregate']['mrr'] == 1 / 3


def test_different_source_ids_do_not_collapse_by_display_name():
    result = evaluate([row(['source-B:1', 'source-A:1'], ['source-A:1'])])
    assert result['aggregate']['mrr'] == .5


@pytest.mark.parametrize('rows', [[], [row([]), row([])],
    [{'qid': 'q', 'ranking': [], 'relevant': []}],
    [{'qid': 'q', 'ranking': None, 'relevant': ['gold']}],
    [row([''])], [row([42])], [row([], ['gold', 'gold'])]])
def test_malformed_or_unlabelled_runs_are_not_silent_zeroes(rows):
    with pytest.raises(ValueError):
        evaluate(rows)
