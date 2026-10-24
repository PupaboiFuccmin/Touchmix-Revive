"""Keep the mixer's and this PC's own names out of saved logs and mapping files.

Mixers and clients carry names their owners chose (the mixer's configured
name, Revive-<computer name>). Logs and probe results are meant to be shared
or committed, so everything the mapping tools write goes through redact().
"""
import threading

MIXER_PLACEHOLDER = 'QSC TouchMix'
CLIENT_PLACEHOLDER = 'Revive-MyPC'

_names = {}
_lock = threading.Lock()


def register(mixer_name=None, client_name=None):
    """Remember names to hide. The client calls this on every connect."""
    with _lock:
        for real, placeholder in ((mixer_name, MIXER_PLACEHOLDER), (client_name, CLIENT_PLACEHOLDER)):
            if real and real != placeholder:
                _names[real] = placeholder
                _names[real.upper()] = placeholder.upper()   # settings file names are upper case


def redact(text):
    with _lock:
        names = sorted(_names.items(), key=lambda kv: -len(kv[0]))   # longest first
    for real, placeholder in names:
        text = text.replace(real, placeholder)
    return text
