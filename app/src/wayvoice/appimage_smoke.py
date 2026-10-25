"""Offline packaging probe; does not start services or capture audio."""
import json
import os
from pathlib import Path
import subprocess


def main():
    import gi
    gi.require_version('Gtk', '4.0')
    gi.require_version('Adw', '1')
    from gi.repository import Gtk, Adw, GdkPixbuf
    import cairo
    import av
    import ctranslate2
    import faster_whisper
    import onnxruntime
    from . import __version__, service, injector
    from .paths import app_dir
    if not Gtk.init_check():
        raise RuntimeError('GTK display unavailable for AppImage smoke test')
    Adw.init()
    window = Adw.Window()
    window.set_content(Gtk.Label(label='WayVoice packaging probe'))
    window.destroy()
    prefix = app_dir().parents[2]
    icon = prefix / 'share/icons/hicolor/scalable/apps/io.github.stepan.WayVoice.svg'
    assert GdkPixbuf.Pixbuf.new_from_file(str(icon)).get_width() > 0
    assert not service.systemd_available()
    root = app_dir().parent
    subprocess.run([str(root / 'parakeet/bin/nemo-speech'), '--version'], check=True)
    subprocess.run(['pw-record', '--help'], check=True, stdout=subprocess.DEVNULL)
    client = injector.ydotool_command()
    if not client:
        raise RuntimeError('Portable paste client unavailable')
    subprocess.run([client, '--help'], check=True, stdout=subprocess.DEVNULL)
    print(json.dumps({'version': __version__, 'python': os.environ.get('WAYVOICE_PYTHON'),
                      'gtk': Gtk.get_minor_version(), 'adw': Adw.get_minor_version(),
                      'runtime_imports': [cairo.version, av.__version__, ctranslate2.__version__,
                                          faster_whisper.__version__, onnxruntime.__version__],
                      'paste_client': client, 'probe': 'runtime imports and resources'}))


if __name__ == '__main__':
    main()
