"""Preparation is distinct from a tested voice-input loop."""
import unittest
from types import SimpleNamespace
try:
    from wayvoice.ui.setup_presentation import setup_steps, model_missing
except (ImportError, ValueError):
    setup_steps = None


@unittest.skipIf(setup_steps is None, 'GTK UI package unavailable')
class SetupStepsTests(unittest.TestCase):
    def steps(self, model=None, **changes):
        reply = {'ok': True, 'engine': {'state': 'ready'}, 'model': model or {}}
        reply.update(changes)
        return dict(setup_steps(reply, {'shortcut': 'F8'}, []))

    def test_runtime_ready_does_not_make_missing_managed_weights_ready(self):
        model = {'supported': True, 'present': False, 'model': 'small'}
        self.assertEqual(self.steps(model)['model_state_row'], 'setup.model_missing')
        self.assertTrue(model_missing({'model': model}))

    def test_foreign_download_does_not_claim_selected_model_is_preparing(self):
        model = {'supported': True, 'present': False, 'model': 'small',
                 'download': {'state': 'downloading', 'model': 'large'}}
        self.assertEqual(self.steps(model)['model_state_row'], 'setup.model_missing')
        model['download']['model'] = 'small'
        self.assertEqual(self.steps(model)['model_state_row'], 'setup.model_pending')

    def test_unmanaged_model_is_not_claimed_verified(self):
        self.assertEqual(self.steps({'supported': False, 'present': True})['model_state_row'],
                         'setup.model_external')

    def test_missing_dependencies_and_disabled_shortcut_have_specific_steps(self):
        steps = dict(setup_steps({'ok': True}, {'shortcut': ''},
                                [SimpleNamespace(id='pipewire'), SimpleNamespace(id='wl-clipboard')]))
        self.assertEqual(steps['dependencies'], 'setup.capture_missing')
        self.assertEqual(steps['paste'], 'setup.clipboard_missing')
        self.assertEqual(steps['shortcut_row'], 'setup.shortcut_disabled')

    def test_supported_backend_does_not_claim_live_shortcut_success(self):
        self.assertEqual(self.steps(shortcut_support=(True, ''))['shortcut_row'],
                         'setup.shortcut_configured')
        self.assertEqual(self.steps()['shortcut_row'], 'setup.shortcut_manual')
        self.assertEqual(setup_steps({'ok': False}, {}, []), [('engine_status_row', 'setup.waiting')])
