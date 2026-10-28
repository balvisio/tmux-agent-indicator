#!/usr/bin/env python3
"""Completion semantics and real hook integration on a disposable tmux server."""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import time
import unittest

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
