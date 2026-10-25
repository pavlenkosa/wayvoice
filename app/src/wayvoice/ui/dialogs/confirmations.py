"""Native confirmations with explicit safe close/default responses."""
from gi.repository import Adw
from ... import model_store
from ...models import display_name


def download_confirmation(window, model_id, size_bytes, t, language, confirmed):
    size = (model_store.human_size(size_bytes, language) if size_bytes > 0
            else t('store.download_unknown_size'))
    name = display_name(model_id)
    dialog = Adw.MessageDialog(transient_for=window, modal=True,
        heading=t('store.download_title'), body=t('store.download_body', name=name, size=size))
    dialog.set_title(t('store.download_title'))
    dialog.add_response('cancel', t('store.download_later'))
    dialog.add_response('download', t('store.download_now'))
    dialog.set_close_response('cancel')
    dialog.set_default_response('download')
    dialog.set_response_appearance('download', Adw.ResponseAppearance.SUGGESTED)
    answered = False
    def response(_dialog, choice):
        nonlocal answered
        if answered:
            return
        answered = True
        if choice == 'download':
            confirmed(dialog, dialog)
    dialog.connect('response', response)
    dialog.present()


def unsaved_confirmation(window, t, respond):
    dialog = Adw.MessageDialog(transient_for=window, modal=True,
        heading=t('settings.unsaved_title'), body=t('settings.unsaved_body'))
    dialog.set_title(t('settings.unsaved_title'))
    for choice, key in (('stay', 'settings.stay'), ('leave', 'settings.leave_unsaved'), ('save', 'settings.save')):
        dialog.add_response(choice, t(key))
    dialog.set_close_response('stay')
    dialog.set_default_response('stay')
    dialog.set_response_appearance('save', Adw.ResponseAppearance.SUGGESTED)
    answered = False
    def response(_dialog, choice):
        nonlocal answered
        if not answered:
            answered = True
            respond(choice)
    dialog.connect('response', response)
    dialog.present()
