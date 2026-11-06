import copy
import json

import pytest

from tools.dfm.errors import DFMError
from tools.dfm.reporting.html.editor_context import (
    REPORT_CONTEXT_MAX_CHARS, editor_runtime, report_editor_context,
)


def result_with(runtime, ids=()):
    return {'ok': True, 'project_id': 'project-a', 'ready': True,
            'run': {'run_id': 'run-a'}, 'runtime': runtime, 'required_issue_ids': list(ids)}


def collect(result):
    cursor, records, pages = None, [], []
    while True:
        page = report_editor_context(result, cursor=cursor)
        assert len(json.dumps(page, ensure_ascii=False)) <= REPORT_CONTEXT_MAX_CHARS
        assert page['report_page']['offset'] == len(records)
        pages.append(page)
        records.extend(page['context_records'])
        cursor = page['report_page']['next_cursor']
        if cursor is None:
            assert page['context_complete']
            assert page['next_action'] == 'render_html'
            break
        assert not page['context_complete']
        assert page['next_call']['arguments']['report_cursor'] == cursor
        assert page['next_action'] == 'report_context'
    return pages, records


def rebuild(records):
    root = {}
    for record in records:
        path, value = record['path'], record['value']
        target = root
        for position, key in enumerate(path[:-1]):
            empty = [] if isinstance(path[position + 1], int) else {}
            if isinstance(target, list):
                while len(target) <= key:
                    target.append(None)
                if target[key] is None:
                    target[key] = empty
            else:
                target.setdefault(key, empty)
            target = target[key]
        key = path[-1]
        if isinstance(target, list):
            while len(target) <= key:
                target.append(None)
        if 'text_offset' in record and record['text_offset']:
            assert len(target[key]) == record['text_offset']
            target[key] += value
        else:
            target[key] = value
    return root


def test_large_geometry_is_editorial_data_without_changing_render_runtime():
    faces = [{'geometry_ref': {'entity_id': f'face-{i}', 'input_sha256': 'a' * 64},
              'status': 'pending_review' if i % 2 else 'assigned',
              'reason': 'needs_confirmation' if i % 2 else 'confirmed',
              'pending_scope_ids': ['LOCAL'] if i % 2 else []} for i in range(10000)]
    runtime = {'resources': {'mesh': 'private-render-data'}, 'report': {
        'stats': {'failed_count': 0, 'indeterminate_count': 4},
        'analysis_coverage': {'status': 'partial', 'forming_assignment_gaps': [{
            'assignment': {'status': 'pending_review', 'faces': faces, 'evaluation_geometry_refs': faces}}]},
        'issues': [], 'result_manifest': {'result_refs': ['r'] * 10000}}}
    before = copy.deepcopy(runtime)
    pages, records = collect(result_with(runtime))
    projected = rebuild(records)['runtime']
    assert runtime == before
    assignment = projected['report']['analysis_coverage']['forming_assignment_gaps'][0]['assignment']
    assert assignment['evaluation_geometry_refs_count'] == 10000
    assert sum(row['face_count'] for row in assignment['face_assignments']) == 10000
    assert {row['status'] for row in assignment['face_assignments']} == {'assigned', 'pending_review'}
    assert projected['report']['analysis_coverage']['status'] == 'partial'
    assert projected['report']['stats']['indeterminate_count'] == 4
    assert 'resources' not in projected and 'result_manifest' not in projected['report']
    assert len(pages) < 4


def test_pages_preserve_every_issue_numeric_criterion_and_long_drawing_note():
    ids = [f'issue-{index}' for index in range(150)]
    note = '表面要求 \\" 保留完整技术注释。\n' * 1000
    runtime = {'drawing_semantics': {'observations': [{'kind': 'global_note', 'value': note}]},
               'report': {'issues': [{'issue_id': identity, 'metric': {
                   'actual': 0.17, 'expected': 0.5, 'operator': '>=', 'unit': 'mm',
                   'rule_id': 'R_TEST', 'outcome': 'failed'}} for identity in ids],
                   'indeterminate_checks': [{'rule_id': 'R_MISSING', 'actual': None, 'reason': 'partial'}]}}
    pages, records = collect(result_with(runtime, ids))
    assert len(pages) > 1
    assert rebuild(records) == {'runtime': editor_runtime(runtime), 'required_issue_ids': ids}
    assert all(page['required_issue_count'] == len(ids) for page in pages)


def test_cursor_is_bound_to_the_report_content_and_run():
    result = result_with({'report': {'issues': [{'issue_id': str(i), 'text': 'x' * 1000} for i in range(10)]}})
    cursor = report_editor_context(result)['report_page']['next_cursor']
    assert cursor
    changed = copy.deepcopy(result)
    changed['runtime']['report']['issues'][0]['text'] = 'modified'
    with pytest.raises(DFMError, match='Report changed'):
        report_editor_context(changed, cursor=cursor)
    changed = copy.deepcopy(result)
    changed['run']['run_id'] = 'other-run'
    with pytest.raises(DFMError, match='Report changed'):
        report_editor_context(changed, cursor=cursor)
    for malformed in ('broken', 'hash:no-number', cursor.rsplit(':', 1)[0] + ':-1'):
        with pytest.raises(DFMError):
            report_editor_context(result, cursor=malformed)


def test_pending_and_terminal_report_context_keep_their_state():
    for result in ({'ready': False, 'next_action': 'report_context'},
                   {'ready': False, 'complete': True, 'next_action': 'result'}):
        assert report_editor_context(result) is result


def test_model_tool_dispatches_all_context_pages(monkeypatch):
    from tools import dfm_tool
    result = result_with({'report': {'issues': [{'issue_id': str(i), 'text': 'x' * 1000} for i in range(20)]}})
    class Service:
        def analysis(self, action, **params):
            assert action == 'report_context'
            return result
    monkeypatch.setattr(dfm_tool, 'get_dfm_service', lambda: Service())
    args = {'action': 'report_context', 'project_id': 'project-a', 'run_id': 'run-a'}
    records = []
    while True:
        output = dfm_tool._call('analysis', args)
        assert len(output) <= REPORT_CONTEXT_MAX_CHARS
        page = json.loads(output)
        records.extend(page['context_records'])
        if page['context_complete']:
            break
        args = page['next_call']['arguments']
    assert rebuild(records)['runtime'] == result['runtime']
