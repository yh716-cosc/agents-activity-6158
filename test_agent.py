"""Offline agent tests: no model requests, Cargo, or project source mutations."""
import io
import json
import os
import pathlib
import tempfile
import unittest
from unittest.mock import patch

import agent


def report(score=100):
    return dict(build=True, cargo_test={'passed': 8, 'failed': 0},
                cargo_test_returncode=0, differential_pct=score,
                differential={k: {'pass': score, 'total': 100} for k in
                              ('parse valid', 'parse invalid', 'compare', 'bump', 'round-trip')},
                spec_precedence_chain=True, violations=[], quality={})


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.lib = self.root / 'lib.rs'
        self.lib.write_text('original', encoding='utf-8')
        self.patches = [patch.object(agent, 'LIB', self.lib),
                        patch.object(agent, 'LOGS', self.root / 'logs')]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        agent.STATE.clear()
        self.history = [{'role': 'system', 'content': agent.system_prompt()},
                        {'role': 'user', 'content': 'Translate'}]

    def evaluate(self, value):
        def run(cmd, **kwargs):
            pathlib.Path(cmd[-1]).write_text(json.dumps(value), encoding='utf-8')
            return {'returncode': 0, 'output': 'evaluation output'}
        with patch.object(agent, '_run', side_effect=run):
            return agent.t_evaluate({})

    def test_checkpoint_rolls_back_regression(self):
        self.evaluate(report(80))
        self.lib.write_text('regressed', encoding='utf-8')
        out = self.evaluate(report(50))
        self.assertTrue(out['rollback'])
        self.assertEqual(self.lib.read_text(), 'original')
        self.assertEqual(agent.STATE['best_score'], 80)

    def test_quality_outranks_score(self):
        self.evaluate(report(80))
        bad = report(100)
        bad['violations'] = ['unsafe_blocks']
        self.lib.write_text('bad', encoding='utf-8')
        self.assertTrue(self.evaluate(bad)['rollback'])

    def test_completion_requires_current_code(self):
        self.evaluate(report())
        self.assertTrue(agent.should_stop(self.history, 1, 40, 100)[0])
        self.lib.write_text('changed', encoding='utf-8')
        self.assertFalse(agent.should_stop(self.history, 1, 40, 100)[0])

    def test_false_success_rejected(self):
        for field, value in [('cargo_test', {'passed': 0, 'failed': 0}),
                             ('cargo_test_returncode', 1), ('violations', ['panic_macros']),
                             ('differential', {}), ('spec_precedence_chain', False)]:
            r = report()
            r[field] = value
            self.assertFalse(agent.passed(r), field)
        r = report()
        r['differential']['compare'] = {'pass': 99999, 'total': 100000}
        self.assertFalse(agent.passed(r))

    def test_context_is_bounded_and_has_no_orphan_tool_ids(self):
        history = self.history + [
            {'role': 'tool', 'name': 'read_file', 'content': 'x' * 100000} for _ in range(100)]
        agent.STATE['notes'] = 'Keep this finding'
        context = agent.build_context(history, 30)
        self.assertLessEqual(sum(len(m['content']) for m in context), agent.CONTEXT_CHARS)
        self.assertNotIn('tool', [m['role'] for m in context])
        self.assertIn('Keep this finding', json.dumps(context))

    def test_dispatch_validates_and_patch_is_unique(self):
        self.assertIn('error', agent.dispatch({'name': 'missing', 'arguments': {}}))
        self.assertIn('error', agent.dispatch({'name': 'write_rust', 'arguments': None}))
        self.assertIn('error', agent.dispatch({'name': 'write_rust', 'arguments': {'content': 2}}))
        self.assertIn('error', agent.dispatch({'name': 'read_file', 'arguments': {'path': '../secret'}}))
        self.assertIn('error', agent.dispatch({'name': 'replace_rust', 'arguments': {'old': '', 'new': 'x'}}))
        agent.dispatch({'name': 'replace_rust', 'arguments': {'old': 'original', 'new': 'updated'}})
        self.assertEqual(self.lib.read_text(), 'updated')

    def test_stopping_conditions(self):
        self.assertTrue(agent.should_stop(self.history, 40, 40, None)[0])
        history = self.history + [{'role': 'assistant', 'tool_calls': []}] * 3
        self.assertTrue(agent.should_stop(history, 3, 40, None)[0])
        history = self.history + [{'role': 'assistant', 'tool_calls': [{'name': 'cargo_build', 'arguments': {}}]}] * 3
        self.assertTrue(agent.should_stop(history, 3, 40, None)[0])
        agent.STATE['stale'] = 4
        self.assertTrue(agent.should_stop(self.history, 4, 40, None)[0])

    def test_api_request_and_normalized_response(self):
        response = {'choices': [{'message': {'content': None, 'tool_calls': [
            {'id': 'call_1', 'function': {'name': 'read_rust', 'arguments': '{}'}}]}}]}
        with patch.dict(os.environ, {'AGENT_PROVIDER': 'openai', 'OPENAI_API_KEY': 'test-secret', 'OPENAI_MODEL': 'test-model'}):
            with patch.object(agent.urllib.request, 'urlopen', return_value=io.BytesIO(json.dumps(response).encode())) as send:
                result = agent.call_model(self.history, agent.SCHEMAS)
                request = json.loads(send.call_args.args[0].data)
        self.assertEqual(result['tool_calls'], [{'name': 'read_rust', 'arguments': {}}])
        self.assertEqual(request['tools'][0]['type'], 'function')
        self.assertNotIn('test-secret', json.dumps(request))

    def test_main_budget_and_full_log(self):
        def run(cmd, **kwargs):
            pathlib.Path(cmd[-1]).write_text(json.dumps(report(70)), encoding='utf-8')
            return {'returncode': 0, 'output': 'x' * 6000}
        with patch.object(agent, 'preflight', return_value=[]), patch.object(agent, '_run', side_effect=run), \
             patch.object(agent, 'call_model', return_value={'text': 'thinking', 'tool_calls': []}) as model, \
             patch.dict(os.environ, {'AGENT_PROVIDER': 'gemini', 'GEMINI_API_KEY': 'mock'}), patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(agent.main(['--budget', '2', '--min-interval', '0']), 1)
        self.assertEqual(model.call_count, 2)
        entries = [json.loads(line) for line in next(agent.LOGS.glob('run-*.jsonl')).read_text().splitlines()]
        self.assertEqual(entries[-1]['event'], 'stop')
        self.assertEqual(len(next(e for e in entries if e['event'] == 'final_evaluation')['result']['output']), 6000)

    def test_gemini_default_endpoint_and_tool_calls(self):
        response = {'choices': [{'message': {'content': None, 'tool_calls': [
            {'function': {'name': 'read_rust', 'arguments': '{}'}}]}}]}
        with patch.dict(os.environ, {'GEMINI_API_KEY': 'gemini-test'}, clear=True), \
             patch.object(agent.urllib.request, 'urlopen', return_value=io.BytesIO(json.dumps(response).encode())) as send:
            result = agent.call_model(self.history, agent.SCHEMAS)
        request = send.call_args.args[0]
        self.assertEqual(request.full_url, 'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions')
        self.assertEqual(request.get_header('Authorization'), 'Bearer gemini-test')
        self.assertEqual(json.loads(request.data)['model'], 'gemini-2.5-flash')
        self.assertNotIn('parallel_tool_calls', json.loads(request.data))
        self.assertEqual(result['tool_calls'][0]['arguments'], {})

    def test_gemini_does_not_use_openai_credentials(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'wrong-provider'}, clear=True):
            with self.assertRaisesRegex(ValueError, 'GEMINI_API_KEY'):
                agent.model_config()

    def test_gemini_model_override(self):
        with patch.dict(os.environ, {'GEMINI_API_KEY': 'test', 'GEMINI_MODEL': 'chosen-model'}, clear=True):
            self.assertEqual(agent.model_config()[2], 'chosen-model')

    def test_rate_limit_is_not_retried(self):
        error = agent.urllib.error.HTTPError('https://example.test', 429, 'limited', {}, None)
        with patch.dict(os.environ, {'GEMINI_API_KEY': 'test'}, clear=True), \
             patch.object(agent.urllib.request, 'urlopen', side_effect=error) as send:
            with self.assertRaisesRegex(RuntimeError, 'quota exhausted'):
                agent.call_model(self.history, agent.SCHEMAS)
        self.assertEqual(send.call_count, 1)

    def test_503_is_classified_without_hidden_retry(self):
        error = agent.urllib.error.HTTPError('https://example.test', 503, 'unavailable', {}, None)
        with patch.dict(os.environ, {'GEMINI_API_KEY': 'test'}, clear=True), \
             patch.object(agent.urllib.request, 'urlopen', side_effect=error) as send:
            with self.assertRaises(agent.TransientModelError):
                agent.call_model(self.history, agent.SCHEMAS)
        self.assertEqual(send.call_count, 1)

    def test_transient_retries_consume_budget_and_recover(self):
        def run(cmd, **kwargs):
            pathlib.Path(cmd[-1]).write_text(json.dumps(report(70)), encoding='utf-8')
            return {'returncode': 0, 'output': 'evaluation'}
        replies = [agent.TransientModelError('503'), {'text': 'recovered', 'tool_calls': []}]
        with patch.object(agent, 'preflight', return_value=[]), patch.object(agent, '_run', side_effect=run), \
             patch.object(agent, 'call_model', side_effect=replies) as model, \
             patch.object(agent.time, 'sleep') as sleep, \
             patch.dict(os.environ, {'AGENT_PROVIDER': 'gemini', 'GEMINI_API_KEY': 'mock'}), \
             patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(agent.main(['--budget', '2', '--min-interval', '0']), 1)
        self.assertEqual(model.call_count, 2)
        sleep.assert_called_once_with(5)
        entries = [json.loads(line) for line in next(agent.LOGS.glob('run-*.jsonl')).read_text().splitlines()]
        self.assertEqual(entries[-1]['steps'], 2)
        self.assertEqual([e['step'] for e in entries if e['event'] == 'model_error'], [1])
        self.assertEqual([e['step'] for e in entries if e['event'] == 'model'], [2])

    def test_transient_retries_are_bounded(self):
        def run(cmd, **kwargs):
            pathlib.Path(cmd[-1]).write_text(json.dumps(report(70)), encoding='utf-8')
            return {'returncode': 0, 'output': 'evaluation'}
        with patch.object(agent, 'preflight', return_value=[]), patch.object(agent, '_run', side_effect=run), \
             patch.object(agent, 'call_model', side_effect=agent.TransientModelError('503')) as model, \
             patch.object(agent.time, 'sleep') as sleep, \
             patch.dict(os.environ, {'AGENT_PROVIDER': 'gemini', 'GEMINI_API_KEY': 'mock'}), \
             patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(agent.main(['--budget', '40', '--min-interval', '0']), 1)
        self.assertEqual(model.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_diagnostics_survive_history_compaction(self):
        agent.STATE['last_diagnostics'] = 'error[E0308]: expected Ordering, found Result'
        history = self.history + [{'role': 'tool', 'name': 'read_file', 'content': 'x' * 10000}] * 20
        context = agent.build_context(history, 25)
        text = '\n'.join(m['content'] for m in context)
        self.assertIn('error[E0308]', text)
        self.assertIn('pub fn compare(a: &Version, b: &Version) -> std::cmp::Ordering', text)
        self.assertLessEqual(len(text), agent.CONTEXT_CHARS + len(context))

    def test_rust_read_reaches_compare_after_line_120(self):
        lines = ['// filler'] * 310
        lines[223] = 'pub fn compare(a: &str, b: &str) -> Result<i8, String> {'
        self.lib.write_text('\n'.join(lines), encoding='utf-8')
        result = agent.dispatch({'name': 'read_rust', 'arguments': {}})
        self.assertIn('224: pub fn compare', result)
        self.assertIn('End of file.', result)
        history = self.history + [{'role': 'tool', 'name': 'read_rust', 'content': result}]
        context = agent.build_context(history, 2)
        self.assertEqual(context[-1]['content'], 'read_rust: ' + result)

    def test_read_page_is_contiguous_with_continuation(self):
        self.lib.write_text('\n'.join('x' * 200 for _ in range(160)), encoding='utf-8')
        result = agent.t_read_file({'path': 'rust/src/lib.rs', 'start': 1, 'end': 160})
        self.assertLessEqual(len(result), 12000)
        self.assertNotIn('omitted', result)
        next_start = int(result.split('Next start: ')[1])
        again = agent.t_read_file({'path': 'rust/src/lib.rs', 'start': next_start})
        self.assertIn(f'\n{next_start}: ', again)
        self.assertIn(f'\n{next_start - 1}: ', result)

    def test_search_source_locates_both_compare_definitions(self):
        self.lib.write_text('    pub fn compare(&self) {}\n\npub fn compare(a: &str) {}', encoding='utf-8')
        result = agent.dispatch({'name': 'search_source', 'arguments':
                                {'path': 'rust/src/lib.rs', 'text': 'pub fn compare'}})
        self.assertIn('2 matching lines', result)
        self.assertIn('3: pub fn compare', result)

    def test_context_includes_current_code_without_read_call(self):
        self.lib.write_text('pub fn compare() { /* current implementation */ }', encoding='utf-8')
        agent.STATE['last_diagnostics'] = 'compare failed in main.rs'
        context = '\n'.join(m['content'] for m in agent.build_context(self.history, 1))
        self.assertIn('current implementation', context)
        self.assertIn('sv::compare', context)

    def test_read_rust_accepts_page_arguments(self):
        self.lib.write_text('\n'.join(str(i) for i in range(200)), encoding='utf-8')
        result = agent.dispatch({'name': 'read_rust', 'arguments': {'start': 150, 'end': 155}})
        self.assertIn('150: 149', result)
        self.assertIn('Next start: 156', result)

    def test_resume_restores_budget_notes_and_best_rank(self):
        agent.STATE.update(steps=23, budget=40, notes='Compare helpers must agree', best_rank=(True, False))
        session = self.root / 'run.state.json'
        agent.save_session(session, self.history)
        agent.STATE.clear()
        history = agent.load_session(session)
        self.assertEqual(agent.STATE['steps'], 23)
        self.assertEqual(agent.STATE['notes'], 'Compare helpers must agree')
        self.assertIsInstance(agent.STATE['best_rank'], tuple)
        self.assertEqual(history[0]['content'], agent.system_prompt())
        self.lib.write_text('external edit', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Rust changed'):
            agent.load_session(session)

    def test_pacing_accounts_for_elapsed_time(self):
        agent.STATE['last_request_at'] = 100
        with patch.object(agent.time, 'time', return_value=104), patch.object(agent.time, 'sleep') as sleep, \
             patch('sys.stdout', new_callable=io.StringIO):
            agent.wait_for_request(13)
        sleep.assert_called_once_with(9)

    def test_edit_triggers_evaluation_without_another_model_call(self):
        evaluations = []
        def run(cmd, **kwargs):
            evaluations.append(self.lib.read_text())
            pathlib.Path(cmd[-1]).write_text(json.dumps(report(70)), encoding='utf-8')
            return {'returncode': 0, 'output': 'evaluation'}
        reply = {'text': None, 'tool_calls': [{'name': 'write_rust', 'arguments': {'content': 'edited'}}]}
        with patch.object(agent, 'preflight', return_value=[]), patch.object(agent, '_run', side_effect=run), \
             patch.object(agent, 'call_model', return_value=reply) as model, \
             patch.dict(os.environ, {'AGENT_PROVIDER': 'gemini', 'GEMINI_API_KEY': 'mock'}), \
             patch('sys.stdout', new_callable=io.StringIO):
            agent.main(['--budget', '1', '--min-interval', '0'])
        self.assertEqual(model.call_count, 1)
        self.assertIn('edited', evaluations)
        entries = [json.loads(line) for line in next(agent.LOGS.glob('run-*.jsonl')).read_text().splitlines()]
        self.assertEqual(len([e for e in entries if e['event'] == 'auto_evaluate']), 1)

    def test_resume_after_api_error_preserves_call_count(self):
        def run(cmd, **kwargs):
            pathlib.Path(cmd[-1]).write_text(json.dumps(report(70)), encoding='utf-8')
            return {'returncode': 0, 'output': 'evaluation'}
        with patch.object(agent, 'preflight', return_value=[]), patch.object(agent, '_run', side_effect=run), \
             patch.dict(os.environ, {'AGENT_PROVIDER': 'gemini', 'GEMINI_API_KEY': 'mock'}), \
             patch('sys.stdout', new_callable=io.StringIO):
            with patch.object(agent, 'call_model', side_effect=RuntimeError('429')):
                agent.main(['--budget', '3', '--used-calls', '1', '--min-interval', '0'])
            session = next(agent.LOGS.glob('*.state.json'))
            self.assertEqual(json.loads(session.read_text())['state']['steps'], 2)
            with patch.object(agent, 'call_model', return_value={'text': '', 'tool_calls': []}) as model:
                agent.main(['--resume', str(session), '--min-interval', '0'])
                self.assertEqual(model.call_count, 1)
            self.assertEqual(json.loads(session.read_text())['state']['steps'], 3)


if __name__ == '__main__':
    unittest.main()

