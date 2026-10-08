"""Value tables behind the settings page dropdowns.

Shared by the legacy page builders and the engine-settings controller: the
dropdowns are filled from these lists and ``_save`` maps the selected index
back onto the stored value, so both sides must read one source of truth.
"""

DEVICES = ["auto", "cpu", "cuda"]
DEVICE_NAMES = ["Auto", "CPU", "NVIDIA CUDA"]
PASTE_MODES = ["standard", "terminal", "copy"]
TIMEOUT_VALUES = [30, 60, 90, 120, 180]
RECORD_VALUES = [30, 60, 120, 300, 600]
UI_LANGUAGE_IDS = ["auto", "ru", "en"]
