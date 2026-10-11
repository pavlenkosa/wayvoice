"""Optional short local cues; playback failures never affect dictation."""
import logging
import math
import struct
import subprocess
import threading

_slot = threading.Lock()


def play_cue(kind, enabled=False, *, wait=False):
    if not enabled or not _slot.acquire(blocking=False):
        return

    def work():
        try:
            frequency = {'start': 880, 'stop': 660, 'cancel': 330}[kind]
            rate, count = 16000, 1280
            pcm = b''.join(struct.pack('<h', int(2500 * math.sin(2 * math.pi * frequency * i / rate)
                                                   * math.sin(math.pi * i / count)))
                           for i in range(count))
            subprocess.run(['pw-play', '--raw', '--format=s16', '--rate=16000',
                            '--channels=1', '-'], input=pcm, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=2, check=False)
        except Exception:
            logging.debug('Audio feedback unavailable', exc_info=True)
        finally:
            _slot.release()
    if wait:
        work()
        return
    try:
        threading.Thread(target=work, daemon=True).start()
    except RuntimeError:
        _slot.release()
