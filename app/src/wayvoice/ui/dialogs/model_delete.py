"""Model-only confirmation; Return defaults to keeping the model."""
from gi.repository import Adw


def delete_confirmation(window, model_id, name, t, confirmed, held=False):
    dialog = Adw.MessageDialog(transient_for=window, modal=True,
        heading=t('store.delete_title'),
        body=t('store.delete_stop_body' if held else 'store.delete_body', name=name))
    dialog.set_title(t('store.delete_title'))
    dialog.add_response('cancel', t('common.cancel'))
    dialog.add_response('delete', t('store.stop_and_delete' if held else 'common.delete'))
    dialog.set_close_response('cancel')
    dialog.set_default_response('cancel')
    dialog.set_response_appearance('delete', Adw.ResponseAppearance.DESTRUCTIVE)
    answered = False
    def response(_dialog, choice):
        nonlocal answered
        if answered:
            return
        answered = True
        if choice == 'delete':
            confirmed(dialog, dialog, model_id)
    dialog.connect('response', response)
    dialog.present()
