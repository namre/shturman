import pytest
from shturman.replies import model

REFS = [{'kind':'chat','source_id':'3','message_id':5,'revision':'hash'}]

def test_reply_cites_only_service_offered_refs():
    result = model.parse({'outcome':'reply','text':'Hello','source_keys':['m:5','m:5']}, REFS)
    assert result.kind == 'reply' and result.source_keys == ('m:5',)

@pytest.mark.parametrize('raw', [None, {'outcome':[]}, {'outcome':'send'},
    {'outcome':'reply','text':' '}, {'outcome':'reply','text':'Hi','owner_approved':True},
    {'outcome':'reply','text':'Hi','source_keys':['r:invented']},
    {'outcome':'reply','text':'Hi','request':{}}, {'outcome':'ask_owner','question':''},
    {'outcome':'need_source','request':{'kind':'chat','source_id':'','reason':'why'}},
    {'outcome':'need_source','request':{'kind':'chat','source_id':'3','limit':True,'reason':'why'}}])
def test_invalid_outcome_is_not_authority(raw):
    with pytest.raises(ValueError): model.parse(raw, REFS)

def test_exact_bounded_source_request():
    out = model.parse({'outcome':'need_source','request':{'kind':'chat','source_id':'3',
        'query':'fact','limit':2,'max_chars':400,'reason':'Need evidence'}}, REFS)
    assert out.request['source_id'] == '3' and out.request['limit'] == 2
    assert set(out.request) == {'kind','source_id','query','since','until','limit','max_chars','reason'}

def test_question_is_bounded_and_normalized():
    assert model.parse({'outcome':'ask_owner','question':'Which\ncontract?'}, []).question == 'Which contract?'
    with pytest.raises(ValueError): model.parse({'outcome':'reply','text':'x'*11}, [], max_chars=10)


def test_global_memory_is_bounded_query_not_a_permission():
    result=model.parse({'outcome':'need_source','request':{'kind':'memory',
        'query':'стоимость проекта','limit':2,'max_chars':500,'reason':'Нужна сумма'}},[])
    assert result.request['source_id'] is None and result.request['query']=='стоимость проекта'
    with pytest.raises(ValueError):
        model.parse({'outcome':'need_source','request':{'kind':'memory','reason':'all'}},[])
