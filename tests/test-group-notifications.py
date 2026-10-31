#!/usr/bin/env python3
"""Completion semantics and real hook integration on a disposable tmux server."""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('notification_state', ROOT / 'scripts/notification-state.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def payload(session, turn, event, prompt=''):
    return dict(session_id=session, prompt_id=turn, turn_id=turn,
                hook_event_name=event, message_hashes=[module.message_hash(prompt)])


class CompletionTest(unittest.TestCase):
    def setUp(self):
        self.live = {'%1': dict(pid='101', window='@1', host='', state='idle'),
                     '%2': dict(pid='102', window='@1', host='%1', state='idle')}
        self.data = {}
        self.n = module.Notifications(self.data, self.live)

    def state(self, pane, state, turn='h1', session='host', prompt=''):
        event = {'running': 'UserPromptSubmit', 'done': 'Stop',
                 'off': 'Interrupt', 'needs-input': 'PermissionRequest'}[state]
        self.n.state(pane, 'claude' if pane == '%1' else 'codex', state,
                     payload(session, turn, event, prompt))
        return self.n.completions()

    def handoff(self, action, turn='h1', text='review'):
        self.n.handoff(dict(action=action, host='%1', host_pid='101', reviewer='%2',
                            session_id='host', prompt_id=turn, hash=module.message_hash(text)))
        return self.n.completions()

    def test_solo_completion_is_immediate_and_duplicate_stop_is_silent(self):
        self.live['%2']['host'] = ''
        self.assertFalse(self.state('%1', 'running'))
        self.assertEqual(len(self.state('%1', 'done')), 1)
        self.assertFalse(self.state('%1', 'done'))

    def test_unrelated_agents_wait_for_each_other(self):
        self.live['%2']['host'] = ''
        self.state('%1', 'running')
        self.state('%2', 'running', 'r1', 'reviewer')
        self.assertFalse(self.state('%1', 'done'))
        self.assertEqual(len(self.state('%2', 'done', 'r1', 'reviewer')), 1)

    def test_pending_review_cannot_expire_into_success(self):
        self.state('%1', 'running')
        self.assertFalse(self.state('%1', 'done'))
        for _ in range(50):
            self.n = module.Notifications(self.data, self.live)
            self.assertFalse(self.n.completions())
        self.handoff('queue')
        self.state('%2', 'running', 'r1', 'reviewer', 'review')
        self.assertEqual(len(self.state('%2', 'done', 'r1', 'reviewer')), 1)

    def test_abandoned_expectation_cancels_without_a_completion_bell(self):
        self.state('%1', 'running')
        self.state('%1', 'done')
        self.assertFalse(self.handoff('cancel'))
        self.assertFalse(self.n.windows['@1']['review_pending'])
        self.assertTrue(self.n.windows['@1']['ready'])
        self.assertEqual(self.n.reviews[module.exchange_id('host', 'h1')]['phase'], 'cancelled')
        self.assertFalse(self.handoff('cancel'))

    def test_abandoned_expectation_preserves_a_newer_completion(self):
        self.state('%1', 'running')
        self.state('%1', 'done')
        self.state('%1', 'running', 'h2')
        self.handoff('skip', 'h2')
        self.state('%1', 'done', 'h2')
        self.assertEqual(len(self.handoff('cancel')), 1)
        self.assertFalse(self.handoff('cancel'))

    def test_cancellation_cannot_release_a_dispatched_review(self):
        self.state('%1', 'running')
        self.handoff('queue')
        self.state('%1', 'done')
        self.assertFalse(self.handoff('cancel'))
        self.assertEqual(self.n.reviews[module.exchange_id('host', 'h1')]['phase'], 'queued')
        self.state('%2', 'running', 'r1', 'reviewer', 'review')
        self.assertFalse(self.handoff('cancel'))
        self.assertEqual(len(self.state('%2', 'done', 'r1', 'reviewer')), 1)

    def test_batched_cancellation_ignores_unknown_and_other_host_reviews(self):
        self.handoff('expect', 'h1')
        self.handoff('expect', 'h2')
        other = dict(self.n.reviews[module.exchange_id('host', 'h1')], host='%2', prompt='other')
        self.n.reviews[module.exchange_id('host', 'other')] = other
        self.n.handoff(dict(action='cancel', host='%1', host_pid='101', session_id='host',
                            prompt_ids=['h1', 'h2', 'unknown', 'other']))
        self.assertEqual(len(self.n.reviews), 3)
        self.assertEqual(other['phase'], 'expected')
        for prompt in ('h1', 'h2'):
            self.assertEqual(self.n.reviews[module.exchange_id('host', prompt)]['phase'], 'cancelled')

    def test_older_review_cannot_release_a_newer_queued_exchange(self):
        self.state('%1', 'running')
        self.handoff('queue', text='first')
        self.state('%1', 'done')
        self.state('%2', 'running', 'r1', 'reviewer', 'first')
        self.state('%1', 'running', 'h2')
        self.handoff('queue', 'h2', 'second')
        self.state('%1', 'done', 'h2')
        self.assertFalse(self.state('%2', 'done', 'r1', 'reviewer'))
        self.state('%2', 'running', 'r2', 'reviewer', 'second')
        self.assertFalse(self.state('%2', 'done', 'r1', 'reviewer'))
        self.assertEqual(len(self.state('%2', 'done', 'r2', 'reviewer')), 1)

    def test_tee_can_finish_before_host_and_skip_does_not_clear_delivery(self):
        self.handoff('expect')
        self.handoff('queue')
        self.state('%2', 'running', 'r1', 'reviewer', 'review')
        self.assertFalse(self.handoff('skip'))
        self.assertFalse(self.state('%2', 'done', 'r1', 'reviewer'))
        self.state('%1', 'running')
        self.assertEqual(len(self.state('%1', 'done')), 1)

    def test_muted_prompt_before_or_after_indicator_hook(self):
        self.handoff('expect')
        self.handoff('skip')
        self.state('%1', 'running')
        self.assertEqual(len(self.state('%1', 'done')), 1)
        self.state('%1', 'running', 'h2')
        self.assertFalse(self.state('%1', 'done', 'h2'))
        self.assertEqual(len(self.handoff('skip', 'h2')), 1)

    def test_interim_stop_and_duplicate_hooks_keep_expectation(self):
        self.state('%1', 'running')
        self.assertFalse(self.state('%1', 'done'))
        self.assertFalse(self.state('%1', 'done'))
        self.handoff('queue')
        self.state('%2', 'running', 'r1', 'reviewer', 'review')
        self.handoff('queue')
        self.assertEqual(len(self.state('%2', 'done', 'r1', 'reviewer')), 1)

    def test_background_notification_keeps_original_review_without_reserving_another(self):
        self.state('%1', 'running')
        self.assertFalse(self.state('%1', 'done'))
        notification = module.hook_data(dict(session_id='host', prompt_id='task-finished',
            hook_event_name='UserPromptSubmit',
            prompt='\n<task-notification><task-id>shell</task-id></task-notification>'))
        self.n.state('%1', 'claude', 'running', notification)
        self.assertFalse(self.n.completions())
        self.assertEqual(self.n.panes['%1']['state'], 'running')
        self.assertEqual(len(self.n.reviews), 1)
        self.assertFalse(self.state('%1', 'done', 'task-finished'))
        self.handoff('queue')
        self.state('%2', 'running', 'r1', 'reviewer', 'review')
        self.assertEqual(len(self.state('%2', 'done', 'r1', 'reviewer')), 1)
        self.assertFalse(self.n.windows['@1']['review_pending'])

    def test_codex_yielded_shell_does_not_finish_its_review(self):
        self.state('%1', 'running')
        self.state('%1', 'done')
        self.handoff('queue')
        self.state('%2', 'running', 'r1', 'reviewer', 'review')
        for call in ('exec_command', 'write_stdin', 'write_stdin'):
            p = module.hook_data(dict(session_id='reviewer', turn_id='r1',
                hook_event_name='PostToolUse', tool_name=call,
                tool_response={'session_id': 123}))
            self.n.state('%2', 'codex', 'running', p)
            self.assertFalse(self.n.completions())
            self.assertEqual(self.n.panes['%2']['state'], 'running')
        self.assertEqual(len(self.state('%2', 'done', 'r1', 'reviewer')), 1)

    def test_task_notification_classification_does_not_swallow_ordinary_prompts(self):
        for text in ('Explain <task-notification>', '"<task-notification>"'):
            with self.subTest(text=text):
                self.setUp()
                p = module.hook_data(dict(session_id='host', prompt_id='one',
                    hook_event_name='UserPromptSubmit', prompt=text))
                self.n.state('%1', 'claude', 'running', p)
                self.assertEqual(len(self.n.reviews), 1)

    def test_permission_resume_cannot_consume_a_pending_review(self):
        self.state('%1', 'running')
        self.state('%2', 'running', 'old', 'reviewer', 'unrelated')
        self.state('%2', 'needs-input', 'old', 'reviewer')
        self.state('%1', 'done')
        self.handoff('queue')
        self.n.state('%2', 'codex', 'running', payload('reviewer', 'old', 'PostToolUse'))
        self.assertFalse(self.state('%2', 'done', 'old', 'reviewer'))
        self.state('%2', 'running', 'r1', 'reviewer', 'review')
        self.assertEqual(len(self.state('%2', 'done', 'r1', 'reviewer')), 1)

    def test_focus_acknowledges_completion_without_cancelling_review(self):
        self.state('%1', 'running')
        self.state('%1', 'done')
        self.n.acknowledge('@1')
        self.assertFalse(self.handoff('skip'))
        self.state('%1', 'running', 'h2')
        self.state('%1', 'done', 'h2')
        self.n.acknowledge('@1')
        self.handoff('queue', 'h2')
        self.state('%2', 'running', 'r1', 'reviewer', 'review')
        self.assertEqual(len(self.state('%2', 'done', 'r1', 'reviewer')), 1)

    def test_delivery_failure_is_not_success_and_late_failure_cannot_undo_receipt(self):
        self.state('%1', 'running')
        self.state('%1', 'done')
        self.handoff('queue')
        self.assertFalse(self.handoff('fail'))
        self.state('%2', 'running', 'r1', 'reviewer', 'review')
        self.handoff('fail')
        self.assertEqual(len(self.state('%2', 'done', 'r1', 'reviewer')), 1)
        self.assertFalse(self.handoff('queue'))

    def test_dead_reviewer_or_reused_pane_never_announces_success(self):
        for replacement in (None, '103'):
            with self.subTest(replacement=replacement):
                self.setUp()
                self.state('%1', 'running')
                self.state('%1', 'done')
                if replacement:
                    self.live['%2']['pid'] = replacement
                else:
                    del self.live['%2']
                self.n = module.Notifications(self.data, self.live)
                self.assertFalse(self.n.completions())
                self.assertFalse(self.n.reviews)

    def test_cancelled_review_does_not_ring(self):
        self.state('%1', 'running')
        self.state('%1', 'done')
        self.handoff('queue')
        self.state('%2', 'running', 'r1', 'reviewer', 'review')
        self.assertFalse(self.state('%2', 'off', 'r1', 'reviewer'))

    def test_interrupt_preserves_another_queued_review(self):
        self.state('%1', 'running')
        self.handoff('queue', text='first')
        self.state('%1', 'done')
        self.state('%2', 'running', 'r1', 'reviewer', 'first')
        self.state('%1', 'running', 'h2')
        self.handoff('queue', 'h2', 'second')
        self.state('%1', 'done', 'h2')
        self.assertFalse(self.state('%2', 'off', 'r1', 'reviewer'))
        self.assertEqual(self.n.reviews[module.exchange_id('host','h2')]['phase'], 'paused')
        self.state('%2', 'running', 'r2', 'reviewer', 'second')
        self.assertEqual(len(self.state('%2', 'done', 'r2', 'reviewer')), 1)

    def test_discarded_draft_does_not_block_a_later_exchange(self):
        self.state('%1', 'running')
        self.handoff('queue', text='first')
        self.state('%1', 'done')
        self.state('%2', 'running', 'r1', 'reviewer', 'first')
        self.state('%1', 'running', 'h2')
        self.handoff('queue', 'h2', 'draft')
        self.state('%1', 'done', 'h2')
        self.assertFalse(self.state('%2', 'off', 'r1', 'reviewer'))
        self.state('%1', 'running', 'h3')
        self.handoff('queue', 'h3', 'new request')
        self.state('%1', 'done', 'h3')
        self.state('%2', 'running', 'r3', 'reviewer', 'new request')
        self.assertEqual(len(self.state('%2', 'done', 'r3', 'reviewer')), 1)

    def test_host_interrupt_does_not_cancel_previous_review(self):
        self.state('%1', 'running')
        self.handoff('queue', text='first')
        self.state('%1', 'done')
        self.state('%2', 'running', 'r1', 'reviewer', 'first')
        self.state('%1', 'running', 'h2')
        self.assertFalse(self.state('%1', 'off', 'h2'))
        old = self.n.reviews[module.exchange_id('host', 'h1')]
        self.assertEqual(old['phase'], 'running')
        self.assertEqual(len(self.state('%2', 'done', 'r1', 'reviewer')), 1)

    def test_identical_messages_are_acknowledged_one_at_a_time(self):
        text = '[kibitz from:claude]\n\nUSER:\nsame'
        self.state('%1', 'running')
        self.handoff('queue', text=text)
        self.state('%1', 'done')
        self.state('%1', 'running', 'h2')
        self.handoff('queue', 'h2', text)
        self.state('%1', 'done', 'h2')
        p = module.hook_data(dict(session_id='reviewer', turn_id='r1',
                                  hook_event_name='UserPromptSubmit', prompt=text))
        self.n.state('%2', 'codex', 'running', p)
        self.n.state('%2', 'codex', 'running', p)
        self.assertFalse(self.state('%2', 'done', 'r1', 'reviewer'))
        self.state('%2', 'running', 'r2', 'reviewer', text)
        self.assertEqual(len(self.state('%2', 'done', 'r2', 'reviewer')), 1)

    def test_late_stop_after_interrupt_cannot_announce_or_recolor_completion(self):
        self.state('%1', 'running')
        self.handoff('queue')
        self.state('%1', 'done')
        self.state('%2', 'running', 'r1', 'reviewer', 'review')
        self.state('%2', 'off', 'r1', 'reviewer')
        self.assertFalse(self.n.state('%2', 'codex', 'done', payload('reviewer', 'r1', 'Stop')))
        self.assertFalse(self.n.completions())
        self.assertEqual(self.n.panes['%2']['state'], 'off')

    def test_disabled_notifications_consume_completions_without_publishing(self):
        self.live['%2']['host'] = ''
        self.state('%1', 'running')
        self.n.state('%1', 'claude', 'done', payload('host', 'h1', 'Stop'))
        self.assertFalse(self.n.completions(enabled=False))
        self.assertFalse(self.n.completions(enabled=True))
        self.assertEqual(self.n.windows['@1']['sequence'], 0)

    def test_same_turn_can_acknowledge_multiple_coalesced_messages(self):
        first = '[kibitz from:claude]\n\nUSER:\none'
        second = '[kibitz from:claude]\n\nUSER:\ntwo'
        self.state('%1', 'running')
        self.handoff('queue', text=first)
        self.state('%1', 'done')
        self.state('%1', 'running', 'h2')
        self.handoff('queue', 'h2', second)
        self.state('%1', 'done', 'h2')
        p = module.hook_data(dict(session_id='reviewer', turn_id='r1',
                                  hook_event_name='UserPromptSubmit', prompt=first+'\n\n'+second))
        self.n.state('%2', 'codex', 'running', p)
        self.assertEqual(len(self.state('%2', 'done', 'r1', 'reviewer')), 1)
        self.assertTrue(all(r['phase'] == 'completed' for r in self.n.reviews.values()))


class StorageIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='notification-storage-')
        self.addCleanup(self.directory.cleanup)
        self.tmp = Path(self.directory.name)
        self.env = {'PATH': os.environ['PATH'], 'HOME': str(self.tmp), 'SHELL': '/bin/bash',
                    'TMUX_TMPDIR': str(self.tmp), 'XDG_CONFIG_HOME': str(self.tmp/'config'),
                    'XDG_CACHE_HOME': str(self.tmp/'cache'), 'XDG_STATE_HOME': str(self.tmp/'state'),
                    'CODEX_HOME': str(self.tmp/'codex'), 'CODEX_CONFIG_DIR': str(self.tmp/'codex'),
                    'ZDOTDIR': str(self.tmp), 'PYTHONDONTWRITEBYTECODE': '1'}
        self.socket = str(self.tmp/'tmux.sock')
        self.tmux('-f', '/dev/null', 'new-session', '-d', '-s', 'test', '/bin/bash --noprofile --norc')
        self.addCleanup(lambda: subprocess.run(['tmux', '-S', self.socket, 'kill-server'],
                                               env=self.env, capture_output=True))
        self.env['TMUX'] = self.tmux('display-message', '-p', '#{socket_path},#{pid},0')
        self.host = self.tmux('display-message', '-p', '#{pane_id}')
        self.window = self.tmux('display-message', '-p', '#{window_id}')
        self.bells = self.tmp/'bells'
        self.tmux('set', '-g', '@agent-indicator-style-active-window', 'on')
        self.tmux('set', '-g', '@agent-indicator-notification-message', 'off')
        self.tmux('set', '-g', '@agent-indicator-notification-command',
                  'printf "%s\\n" "$AGENT_STATE" >> '+shlex.quote(str(self.bells)))

    def tmux(self, *args):
        return subprocess.check_output(['tmux', '-S', self.socket, *args], env=self.env,
                                       text=True, stderr=subprocess.STDOUT, timeout=10).strip()

    def hook(self, pane, state, data, env=None):
        return subprocess.run([str(ROOT/'scripts/agent-state.sh'), '--hook', '--agent', 'claude',
                               '--state', state], input=json.dumps(data),
                              env=dict(env or self.env, TMUX_PANE=pane), text=True,
                              check=True, capture_output=True, timeout=20)

    def tracking(self):
        return json.loads(self.tmux('show-option', '-gqv', module.OPTION))

    def test_large_history_preserves_grace_and_grouped_completion(self):
        reviewer = self.tmux('split-window', '-d', '-P', '-F', '#{pane_id}',
                             '/bin/bash --noprofile --norc')
        self.tmux('set', '-p', '-t', reviewer, '@kibitz_host_pane', self.host)
        old = dict(host=self.host, reviewer=reviewer,
                   host_pid=self.tmux('display-message', '-p', '-t', self.host, '#{pane_pid}'),
                   reviewer_pid=self.tmux('display-message', '-p', '-t', reviewer, '#{pane_pid}'),
                   session='11111111-2222-3333-4444-555555555555', phase='completed',
                   hash='a'*64, turn='22222222-3333-4444-5555-666666666666:previous')
        reviews = {str(i): dict(old, prompt=f'previous-{i}-'+'x'*220) for i in range(128)}
        initial = json.dumps({'reviews': reviews})
        self.assertGreater(len(initial), 65536)
        subprocess.run(['tmux', '-S', self.socket, 'source-file', '-'], env=self.env,
                       input='set -g '+module.OPTION+' '+shlex.quote(initial)+'\n', text=True,
                       check=True, capture_output=True, timeout=10)
        self.tmux('set', '-g', '@agent-indicator-input-delay', '30')

        host_data = dict(session_id='host', prompt_id='new', hook_event_name='UserPromptSubmit')
        self.hook(self.host, 'running', host_data)
        host_data['hook_event_name'] = 'Stop'
        self.hook(self.host, 'done', host_data)
        self.assertEqual(self.tmux('show-option', '-wqv', '@agent-indicator-state'), 'running')
        handoff = dict(old, action='queue', session_id='host', prompt_id='new',
                       hash=module.message_hash('review'))
        subprocess.run([str(ROOT/'scripts/agent-notify.sh')], input=json.dumps(handoff),
                       env=self.env, text=True, check=True, capture_output=True, timeout=20)
        data = dict(session_id='reviewer', turn_id='new-review', hook_event_name='UserPromptSubmit',
                    prompt='review')
        self.hook(reviewer, 'running', data)
        data.update(hook_event_name='PermissionRequest', tool_use_id='tool')
        self.hook(reviewer, 'needs-input', data)
        self.assertFalse(self.tracking()['questions'][reviewer]['shown'])
        self.assertEqual(self.tmux('show-option', '-wqv', '@agent-indicator-state'), 'running')
        self.assertFalse(self.bells.exists())
        data['hook_event_name'] = 'PostToolUse'
        self.hook(reviewer, 'running', data)
        self.assertNotIn(reviewer, self.tracking()['questions'])
        data['hook_event_name'] = 'Stop'
        self.hook(reviewer, 'done', data)
        self.assertEqual(self.tmux('show-option', '-wqv', '@agent-indicator-state'), 'done')
        self.assertIn('bg=red', self.tmux('show-option', '-wqv', 'window-status-style'))
        self.assertTrue(self.tracking()['windows'][self.window]['ready'])
        self.assertEqual(self.tracking()['reviews'][module.exchange_id('host', 'new')]['phase'], 'completed')
        self.assertGreater(len(json.dumps(self.tracking())), 65536)
        for _ in range(50):
            if self.bells.exists():
                break
            time.sleep(.02)
        self.assertEqual(self.bells.read_text(), 'done\n')

    def test_streamed_options_preserve_literal_values(self):
        value = 'x'*80000 + " '$HOME' \\\n\\\"; set -g @injected yes; # ☃ #{pane_id}"
        with patch.dict(os.environ, self.env, clear=True):
            module.tmux_commands([['set-option', '-g', '@literal', value],
                                  ['set-option', '-g', '@empty', '']])
        self.assertEqual(self.tmux('show-option', '-gqv', '@literal'), value)
        self.assertEqual(self.tmux('show-option', '-gqv', '@empty'), '')
        self.assertEqual(self.tmux('show-option', '-gqv', '@injected'), '')

    def test_kibitz_cancels_abandoned_reviews_without_losing_new_completion(self):
        kibitz = Path(os.environ.get('KIBITZ_TEST_REPO', ROOT.parent / 'kibitz'))
        if not (kibitz/'hooks/kibitz_notifications.py').exists():
            self.skipTest('set KIBITZ_TEST_REPO')
        reviewer = self.tmux('split-window', '-d', '-P', '-F', '#{pane_id}',
                             '/bin/bash --noprofile --norc')
        self.tmux('set', '-p', '-t', reviewer, '@kibitz_host_pane', self.host)
        self.tmux('set', '-p', '-t', reviewer, '@name', 'codex')
        transcript = self.tmp/'transcript.jsonl'
        def run(name, data):
            subprocess.run([sys.executable, str(kibitz/'hooks'/name)], input=json.dumps(data),
                           env=dict(self.env, TMUX_PANE=self.host, TMUX_AGENT_INDICATOR_DIR=str(ROOT)),
                           text=True, check=True, capture_output=True, timeout=20)
        for number, kind in enumerate(('replacement', 'TaskStop'), 1):
            old, new = kind+'-old', kind+'-new'
            entries = [dict(type='user', promptId=old, uuid=old, parentUuid='anchor',
                            message={'content': 'old question'})]
            data = dict(session_id='host', prompt_id=old, prompt='old question',
                        hook_event_name='UserPromptSubmit', transcript_path=str(transcript))
            run('kibitz_hook_user_prompt_submit.py', data)
            self.hook(self.host, 'running', data)
            if kind == 'TaskStop':
                entries.append(dict(type='user', toolUseResult={'backgroundTaskId':'task'},
                                    message={'content': []}))
            self.hook(self.host, 'done', dict(data, hook_event_name='Stop'))
            entries.append(dict(type='user', promptId=new, uuid=new,
                                parentUuid='anchor' if kind == 'replacement' else old,
                                message={'content': 'new question /mute'}))
            if kind == 'TaskStop':
                entries.extend([
                    dict(type='assistant', message={'content':[dict(type='tool_use', id='stop',
                        name='TaskStop', input={'task_id':'task'})]}),
                    dict(type='user', message={'content':[dict(type='tool_result', tool_use_id='stop',
                        is_error=False)]}, toolUseResult={'task_id':'task'}),
                ])
            transcript.write_text(''.join(json.dumps(e)+'\n' for e in entries))
            data.update(prompt_id=new, prompt='new question /mute')
            run('kibitz_hook_user_prompt_submit.py', data)
            self.hook(self.host, 'running', data)
            data.update(hook_event_name='Stop', last_assistant_message='answer', background_tasks=[])
            self.hook(self.host, 'done', data)
            self.assertEqual(self.tmux('show-option', '-wqv', '@agent-indicator-state'), 'running')
            run('kibitz_hook_stop.py', data)
            self.assertEqual(self.tracking()['reviews'][module.exchange_id('host', old)]['phase'], 'cancelled')
            self.assertFalse(self.tracking()['windows'][self.window]['review_pending'])
            self.assertEqual(self.tmux('show-option', '-wqv', '@agent-indicator-state'), 'done')
            for _ in range(50):
                if self.bells.exists() and self.bells.read_text().count('done\n') == number:
                    break
                time.sleep(.02)
            self.assertEqual(self.bells.read_text(), 'done\n'*number)

    def test_failed_write_cannot_publish_a_color_or_ring(self):
        data = dict(session_id='host', prompt_id='new', hook_event_name='UserPromptSubmit')
        self.hook(self.host, 'running', data)
        before = self.tracking()
        self.tmux('set', '-g', '@agent-indicator-input-delay', '0')
        bindir = self.tmp/'bin'
        bindir.mkdir()
        wrapper = bindir/'tmux'
        wrapper.write_text('#!/bin/sh\nif [ "$1" = source-file ]; then echo "fixture write failed" >&2; exit 1; fi\n'
                           'exec '+shlex.quote(shutil.which('tmux'))+' "$@"\n')
        wrapper.chmod(0o755)
        data['hook_event_name'] = 'PermissionRequest'
        result = self.hook(self.host, 'needs-input', data,
                           dict(self.env, PATH=str(bindir)+os.pathsep+self.env['PATH']))
        self.assertIn('fixture write failed', result.stderr)
        self.assertEqual(self.tracking(), before)
        self.assertEqual(self.tmux('show-environment', '-g', f'TMUX_AGENT_PANE_{self.host}_STATE'),
                         f'TMUX_AGENT_PANE_{self.host}_STATE=running')
        self.assertEqual(self.tmux('show-option', '-wqv', '@agent-indicator-state'), 'running')
        self.assertFalse(self.bells.exists())


class HookIntegrationTest(unittest.TestCase):
    def test_real_hook_scripts_share_one_completion_event(self):
        kibitz = Path(os.environ.get('KIBITZ_TEST_REPO', ROOT.parent / 'kibitz'))
        if not (kibitz/'hooks/kibitz_notifications.py').exists():
            self.skipTest('matching Kibitz checkout needed; set KIBITZ_TEST_REPO')
        with tempfile.TemporaryDirectory(prefix='group-notification-') as directory:
            tmp = Path(directory)
            env = {'PATH': os.environ['PATH'], 'HOME': str(tmp), 'SHELL': '/bin/bash',
                   'TMUX_TMPDIR': str(tmp), 'XDG_CONFIG_HOME': str(tmp/'config'),
                   'XDG_CACHE_HOME': str(tmp/'cache'), 'XDG_STATE_HOME': str(tmp/'state'),
                   'CODEX_HOME': str(tmp/'codex'), 'CODEX_CONFIG_DIR': str(tmp/'codex'),
                   'ZDOTDIR': str(tmp), 'TMUX_AGENT_INDICATOR_DIR': str(ROOT),
                   'PYTHONDONTWRITEBYTECODE': '1'}
            socket = str(tmp/'tmux.sock')
            def tmux(*args):
                return subprocess.check_output(['tmux', '-S', socket, *args], env=env,
                                               text=True, stderr=subprocess.STDOUT, timeout=10).strip()
            def run(script, data, *args, pane):
                e = dict(env, TMUX_PANE=pane)
                subprocess.run([str(script), *args], input=json.dumps(data), env=e, text=True,
                               check=True, capture_output=True, timeout=20)
            try:
                tmux('-f', '/dev/null', 'new-session', '-d', '-s', 'test', '/bin/bash --noprofile --norc')
                env['TMUX'] = tmux('display-message', '-p', '#{socket_path},#{pid},0')
                host = tmux('display-message', '-p', '#{pane_id}')
                window = tmux('display-message', '-p', '#{window_id}')
                reviewer = tmux('split-window', '-d', '-P', '-F', '#{pane_id}', '/bin/bash --noprofile --norc')
                tmux('set', '-p', '-t', reviewer, '@name', 'codex')
                tmux('set', '-p', '-t', reviewer, '@kibitz_host_pane', host)
                log = tmp/'bells'
                tmux('set', '-g', '@agent-indicator-notification-message', 'off')
                tmux('set', '-g', '@agent-indicator-input-delay', '0')
                tmux('set', '-g', '@agent-indicator-notification-command',
                     'printf "%s\\n" "$AGENT_STATE" >> '+shlex.quote(str(log)))
                tmux('set', '-g', '@agent-indicator-style-active-window', 'on')
                for state, color in [('running', 'yellow'), ('needs-input', 'magenta'), ('done', 'red')]:
                    tmux('set', '-g', f'@agent-indicator-{state}-window-title-bg', color)
                    tmux('set', '-g', f'@agent-indicator-{state}-window-title-fg', 'black')

                def assert_color(state, color):
                    # Both local and remote dots consume this state and title style.
                    self.assertEqual(tmux('show-option', '-wqv', '-t', window, '@agent-indicator-state'), state)
                    for option in ('window-status-style', 'window-status-current-style'):
                        self.assertIn('bg='+color, tmux('show-option', '-wqv', '-t', window, option))

                def assert_pending(value):
                    self.assertEqual(tmux('show-option', '-wqv', '-t', window,
                                          '@agent-indicator-review-pending'), str(value))
                # The transport is inert; the real Kibitz hooks still register the
                # exact message they would deliver, without touching another pane.
                bindir = tmp/'bin'; bindir.mkdir()
                (bindir/'tmux-bridge').write_text('#!/bin/sh\nexit 0\n')
                (bindir/'tmux-bridge').chmod(0o755)
                env['PATH'] = str(bindir)+os.pathsep+env['PATH']
                transcript = tmp/'claude.jsonl'
                transcript.write_text(json.dumps(dict(type='user', promptId='h1',
                    origin={'kind':'human'}, message={'role':'user','content':'fixture'}))+'\n')
                data = dict(session_id='host', prompt_id='h1', prompt='fixture',
                            hook_event_name='UserPromptSubmit', transcript_path=str(transcript))
                run(kibitz/'hooks/kibitz_hook_user_prompt_submit.py', data, pane=host)
                run(ROOT/'scripts/agent-state.sh', data, '--hook','--agent','claude','--state','running',pane=host)
                data.update(hook_event_name='Stop', last_assistant_message='answer', background_tasks=[])
                run(ROOT/'scripts/agent-state.sh', data,'--hook','--agent','claude','--state','done',pane=host)
                self.assertFalse(log.exists())
                assert_color('running', 'yellow')
                assert_pending(1)
                self.assertEqual(tmux('show-environment', '-g', f'TMUX_AGENT_PANE_{host}_STATE'),
                                 f'TMUX_AGENT_PANE_{host}_STATE=done')
                tmux('set', '-g', '@agent-indicator-running-enabled', 'off')
                run(ROOT/'scripts/window-state.sh', {}, window, pane=host)
                assert_color('done', 'red')
                tmux('set', '-g', '@agent-indicator-running-enabled', 'on')
                run(ROOT/'scripts/window-state.sh', {}, window, pane=host)
                assert_color('running', 'yellow')
                run(ROOT/'scripts/pane-focus-in.sh', {}, host, window, pane=host)
                assert_color('running', 'yellow')
                run(kibitz/'hooks/kibitz_hook_stop.py', data,pane=host)
                self.assertFalse(log.exists())
                assert_color('running', 'yellow')
                reviewer_data = dict(session_id='reviewer', turn_id='r1', hook_event_name='UserPromptSubmit',
                                     prompt='[kibitz from:claude]\n\nUSER:\nfixture\n\nCLAUDE:\nanswer')
                # The state protocol is common to both agents; Claude's direct pane
                # targeting lets this test invoke it without a live model process.
                run(ROOT/'scripts/agent-state.sh',reviewer_data,'--hook','--agent','claude','--state','running',pane=reviewer)
                self.assertFalse(log.exists())
                assert_color('running', 'yellow')
                reviewer_data['hook_event_name']='PermissionRequest'
                run(ROOT/'scripts/agent-state.sh',reviewer_data,'--hook','--agent','claude','--state','needs-input',pane=reviewer)
                assert_color('needs-input', 'magenta')
                reviewer_data['hook_event_name']='PostToolUse'
                run(ROOT/'scripts/agent-state.sh',reviewer_data,'--hook','--agent','claude','--state','running',pane=reviewer)
                assert_color('running', 'yellow')
                reviewer_data['hook_event_name']='Stop'
                run(ROOT/'scripts/agent-state.sh',reviewer_data,'--hook','--agent','claude','--state','done',pane=reviewer)
                for _ in range(50):
                    if log.exists() and 'done' in log.read_text(): break
                    time.sleep(.02)
                self.assertEqual(log.read_text(), 'needs-input\ndone\n')
                assert_color('done', 'red')
                assert_pending(0)
                event = tmux('show-option','-wqv','@agent-indicator-completion')
                self.assertEqual(event.split(':')[1:], ['1', '1'], event)
                run(ROOT/'scripts/agent-state.sh',reviewer_data,'--hook','--agent','claude','--state','done',pane=reviewer)
                self.assertEqual(log.read_text(), 'needs-input\ndone\n')

                # Replay real background-task wakeups through both hook orders.
                # Each gets a new Claude prompt ID, but only the human prompt is
                # forwarded. The reviewer may also yield a long-running tool.
                for index, indicator_first in enumerate((False, True)):
                    human = f'background-{index}'
                    task = f'shell-{index}'
                    notice = f'<task-notification><task-id>{task}</task-id></task-notification>'
                    entries = [dict(type='user', promptId=human,
                                    message={'role':'user', 'content':'run a shell task'})]
                    transcript.write_text(json.dumps(entries[0])+'\n')
                    data = dict(session_id='host', prompt_id=human, prompt='run a shell task',
                                hook_event_name='UserPromptSubmit', transcript_path=str(transcript))

                    def submit_background():
                        calls = [(ROOT/'scripts/agent-state.sh',
                                  ('--hook', '--agent', 'claude', '--state', 'running')),
                                 (kibitz/'hooks/kibitz_hook_user_prompt_submit.py', ())]
                        if not indicator_first:
                            calls.reverse()
                        for script, args in calls:
                            run(script, data, *args, pane=host)

                    submit_background()
                    entries.append(dict(type='user', toolUseResult={'backgroundTaskId':task},
                        message={'role':'user', 'content':[{'type':'tool_result', 'content':'running'}]}))
                    transcript.write_text(''.join(json.dumps(e)+'\n' for e in entries))
                    data.update(hook_event_name='Stop', last_assistant_message='task running',
                                background_tasks=[{'id':task, 'status':'running', 'type':'shell'}])
                    before = log.read_text()
                    run(ROOT/'scripts/agent-state.sh', data,'--hook','--agent','claude','--state','done',pane=host)
                    run(kibitz/'hooks/kibitz_hook_stop.py', data, pane=host)
                    assert_color('running', 'yellow')
                    self.assertEqual(log.read_text(), before)

                    data.update(prompt_id=human+'-wake', prompt=notice, hook_event_name='UserPromptSubmit')
                    entries.append(dict(type='user', promptId=data['prompt_id'],
                        origin={'kind':'task-notification'}, message={'role':'user', 'content':notice}))
                    transcript.write_text(''.join(json.dumps(e)+'\n' for e in entries))
                    submit_background()
                    assert_color('running', 'yellow')
                    data.update(hook_event_name='Stop', last_assistant_message='task finished', background_tasks=[])
                    run(ROOT/'scripts/agent-state.sh', data,'--hook','--agent','claude','--state','done',pane=host)
                    run(kibitz/'hooks/kibitz_hook_stop.py', data, pane=host)
                    assert_color('running', 'yellow')
                    tracking = json.loads(tmux('show-option','-gqv',module.OPTION))
                    self.assertNotIn(module.exchange_id('host', human+'-wake'), tracking['reviews'])
                    review = dict(session_id='reviewer', turn_id='review-'+human,
                        hook_event_name='UserPromptSubmit',
                        prompt='[kibitz from:claude]\n\nUSER:\nrun a shell task\n\nCLAUDE:\ntask finished')
                    run(ROOT/'scripts/agent-state.sh', review,'--hook','--agent','claude','--state','running',pane=reviewer)
                    review.update(hook_event_name='PostToolUse', tool_response={'session_id':123})
                    run(ROOT/'scripts/agent-state.sh', review,'--hook','--agent','claude','--state','running',pane=reviewer)
                    assert_color('running', 'yellow')
                    self.assertEqual(log.read_text(), before)
                    review['hook_event_name'] = 'Stop'
                    run(ROOT/'scripts/agent-state.sh', review,'--hook','--agent','claude','--state','done',pane=reviewer)
                    assert_color('done', 'red')
                    assert_pending(0)
                    for _ in range(50):
                        if log.read_text() != before: break
                        time.sleep(.02)
                    self.assertEqual(log.read_text(), before+'done\n')

                # A late skip must release yellow immediately, without a new state hook.
                data.update(prompt_id='h2', prompt='fixture /mute', hook_event_name='UserPromptSubmit')
                run(ROOT/'scripts/agent-state.sh', data,'--hook','--agent','claude','--state','running',pane=host)
                data['hook_event_name']='Stop'
                run(ROOT/'scripts/agent-state.sh', data,'--hook','--agent','claude','--state','done',pane=host)
                assert_color('running', 'yellow')
                skip = dict(action='skip', host=host, host_pid=tmux('display-message','-p','-t',host,'#{pane_pid}'),
                            reviewer=reviewer, session_id='host', prompt_id='h2')
                run(ROOT/'scripts/agent-notify.sh', skip, pane=host)
                assert_color('done', 'red')
                assert_pending(0)

                # Muting sounds does not disable review-aware colors or interruption cleanup.
                tmux('set', '-g', '@agent-indicator-notification-enabled', 'off')
                run(ROOT/'scripts/pane-focus-in.sh', {}, host, window, pane=host)
                data.update(prompt_id='h3', prompt='fixture', hook_event_name='UserPromptSubmit')
                run(ROOT/'scripts/agent-state.sh', data,'--hook','--agent','claude','--state','running',pane=host)
                assert_color('running', 'yellow')
                data['hook_event_name']='Interrupt'
                run(ROOT/'scripts/agent-state.sh', data,'--hook','--agent','claude','--state','off',pane=host)
                assert_pending(0)
                self.assertEqual(tmux('show-option','-wqv','-t',window,'@agent-indicator-state'), 'idle')

                # Closing the reviewer removes the busy overlay on the next sweep.
                data.update(prompt_id='h4', hook_event_name='UserPromptSubmit')
                run(ROOT/'scripts/agent-state.sh', data,'--hook','--agent','claude','--state','running',pane=host)
                data['hook_event_name']='Stop'
                run(ROOT/'scripts/agent-state.sh', data,'--hook','--agent','claude','--state','done',pane=host)
                assert_color('running', 'yellow')
                tmux('kill-pane', '-t', reviewer)
                run(ROOT/'scripts/window-state.sh', {}, window, pane=host)
                assert_pending(0)
                assert_color('done', 'red')
                run(ROOT/'scripts/reset_all.sh', {}, pane=host)
                self.assertEqual(tmux('show-option','-wqv','-t',window,'@agent-indicator-review-pending'), '')
            finally:
                subprocess.run(['tmux','-S',socket,'kill-server'],env=env,capture_output=True)


if __name__ == '__main__':
    unittest.main()
