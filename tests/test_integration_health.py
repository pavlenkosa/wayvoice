"""Passive checks never claim an external application accepted input."""
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
from unittest import mock
from wayvoice import integration_health as health


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'helper.sock'
        for p in (mock.patch.dict(os.environ, {'WAYLAND_DISPLAY':'test'}),
                  mock.patch.object(health.shutil,'which',return_value='/bin/wl-copy'),
                  mock.patch.object(health.injector,'ydotool_command',return_value='/bin/ydotool'),
                  mock.patch.object(health.injector,'ydotool_socket_candidates',return_value=(self.path,)),
                  mock.patch.object(health.shortcut,'manual_shortcut_required',return_value=True),
                  mock.patch.object(health.os,'access',return_value=False)):
            p.start(); self.addCleanup(p.stop)

    def test_live_socket_wins_over_stale_and_uinput_denial(self):
        self.path.touch()
        live = self.path.with_name('live.sock')
        with socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM) as server:
            server.bind(str(live))
            with mock.patch.object(health.injector,'ydotool_socket_candidates',return_value=(self.path,live)):
                result=health.collect_health({})
        self.assertEqual(result['socket'],str(live))
        self.assertEqual(result['delivery'],'helper_available')
        self.assertEqual(result['reason'],'prerequisites')

    def test_missing_stale_and_denied_remain_distinct(self):
        for state in ('missing','stale','permission_denied','timeout'):
            with self.subTest(state=state), mock.patch.object(health,'_socket_state',return_value=state):
                result=health.collect_health({})
            self.assertEqual(result['helper'],state)
            self.assertEqual(result['reason'],'helper_'+state)
            self.assertEqual(result['delivery'],'copy_only')

    def test_total_budget_expiry_is_unknown_not_missing(self):
        with mock.patch.object(health.time,'monotonic',side_effect=[0,0,4]):
            result=health.collect_health({},timeout=3)
        self.assertFalse(result['complete'])
        self.assertEqual(result['delivery'],'unknown')
        self.assertEqual(result['reason'],'timeout')

    def test_readback_quotes_command_and_never_writes(self):
        outputs=[repr([health.shortcut.KEY]),repr('<Control>F8'),repr("'/opt/Voice App/wayvoice' toggle")]
        with mock.patch.object(health.shortcut,'manual_shortcut_required',return_value=False), \
             mock.patch.object(health.shortcut,'manual_command',return_value="'/opt/Voice App/wayvoice' toggle"), \
             mock.patch.object(health.subprocess,'run',side_effect=[subprocess.CompletedProcess([],0,s) for s in outputs]) as run:
            result=health.collect_health({'shortcut':'<Primary>F8'})
        self.assertEqual(result['shortcut'],'matched')
        self.assertTrue(all(c.args[0][:2]==['gsettings','get'] for c in run.call_args_list))
        self.assertTrue(all(0<c.kwargs['timeout']<=3 for c in run.call_args_list))

    def test_passive_check_cannot_start_or_send(self):
        with mock.patch.object(health.injector,'inject') as inject, \
             mock.patch.object(health.injector,'copy_to_clipboard') as copy, \
             mock.patch.object(health.injector,'ensure_helper_running') as start:
            result=health.collect_health({'paste_mode':'copy'})
        self.assertEqual(result['reason'],'copy_only')
        inject.assert_not_called(); copy.assert_not_called(); start.assert_not_called()
