from __future__ import annotations

import json
import locale
import os
from importlib.resources import files
import warnings
from typing import Any

from . import languages

UI_LANGUAGE_NAMES = {
    "ru": "Русский", "en": "English", "es": "Español", "pt": "Português",
    "fr": "Français", "de": "Deutsch", "zh": "简体中文", "ja": "日本語",
    "ar": "العربية", "hi": "हिन्दी",
}
SUPPORTED_UI_LANGUAGES = ("auto", *UI_LANGUAGE_NAMES)

_RU = {
    "nav.home": "Главная",
    "nav.settings": "Настройки",
    "menu.update": "Проверить обновления…",
    "menu.about": "О программе",
    "menu.diagnostics": "Скопировать диагностику",
    "menu.quit": "Выйти",
    "home.title": "Голосовой ввод",
    "home.subtitle": "Диктуй текст в любое приложение.",
    "status.starting": "Запуск…",
    "hero.starting": "Запускаю…",
    "hero.record": "Нажми и говори",
    "hero.recording": "Слушаю…",
    "hero.recording_hint": "Нажми ещё раз, когда закончишь.",
    "hero.transcribing": "Распознаю речь…",
    "hero.cancel_hint": "Нажми, чтобы отменить распознавание.",
    "hero.not_ready": "Распознавание не готово",
    "hero.open_settings": "Открой настройки.",
    "hero.downloading": "Скачивается {model}",
    "hero.warming": "Подготовка {model}",
    "shortcut.global": "Глобальная клавиша",
    "card.engine": "Движок",
    "card.model": "Модель",
    "card.paste": "Вставка",
    "health.title": "Состояние",
    "health.checking": "Проверяю…",
    "health.ready": "готово",
    "health.not_ready": "не готово",
    "health.error": "ошибка",
    "health.warning": "предупреждение",
    "health.deps_missing": "Не хватает зависимостей: {names}",
    "transcript.title": "Последний текст",
    "transcript.empty": "Здесь появится последняя диктовка.",
    "transcript.last": "последний результат",
    "status.recording": "Запись",
    "status.transcribing": "Распознаю",
    "status.ready": "Готов",
    "status.engine": "Движок",
    "status.preparing": "Подготовка",
    "status.waiting": "ожидание",
    "status.start": "Запуск",
    "settings.recognition": "Распознавание",
    "settings.engine": "Движок",
    "settings.engine_state": "Состояние",
    "settings.prepare": "Подготовить",
    "settings.repair": "Исправить",
    "settings.preparing": "Подготавливаю…",
    "settings.model": "Модель",
    "settings.custom_model": "Hugging Face repo или локальный путь",
    "settings.device": "Вычислитель",
    "settings.vad": "Отсекать тишину",
    "settings.worker": "Тёплый распознаватель",
    "settings.worker_sub": "Модель остаётся в памяти между записями — быстрее появляется текст. Занимает несколько ГБ ОЗУ.",
    "settings.parakeet_prepare": "Скачать CPU-runtime и модель (~690 МиБ)",
    "settings.parakeet_binary": "Путь к nemo-speech v0.2.0 (CPU)",
    "settings.parakeet_model": "Путь к Parakeet v3 GGUF (язык определяется автоматически)",
    "settings.cpp_binary": "Путь к whisper-cli",
    "settings.cpp_model": "Путь к модели",
    "settings.cpp_gpu": "Использовать GPU",
    "settings.custom_command": "Команда распознавания",
    "settings.custom_command_sub": "Выполняется через /bin/sh; {audio} — путь к записи. Текст печатается в stdout.",
    "settings.text": "Язык и текст",
    "settings.language": "Язык распознавания",
    "settings.punctuation": "Пунктуация",
    "settings.spoken": "Голосовые команды",
    "settings.spoken_sub": "точка, запятая, новая строка, новый абзац",
    "settings.append_space": "Пробел после вставки",
    "settings.control": "Управление",
    "settings.shortcut": "Глобальная клавиша",
    "settings.change": "Изменить",
    "settings.paste": "Вставка",
    "settings.notifications": "Уведомления",
    "settings.notification_text": "Текст диктовки в уведомлениях",
    "settings.notification_text_sub": "Выключено по умолчанию: текст может остаться в истории уведомлений.",
    "settings.safety": "Ограничения",
    "settings.timeout": "Тайм-аут распознавания",
    "settings.timeout_sub": "Останавливает зависший или слишком долгий запуск модели",
    "settings.max_recording": "Максимальная запись",
    "settings.max_recording_sub": "Запись автоматически завершится и отправится на распознавание",
    "settings.interface": "Интерфейс",
    "settings.ui_language": "Язык интерфейса",
    "settings.language_pending": "Сохранено. Сохраните оставшиеся изменения для применения языка интерфейса.",
    "settings.unsaved_title": "Несохранённые изменения",
    "settings.unsaved_body": "Есть несохранённые изменения. Сохранить?\nИзменения модели применятся к диктовке после сохранения.",
    "settings.stay": "Остаться",
    "settings.leave_unsaved": "Перейти без сохранения",
    "settings.save": "Сохранить",
    "settings.saved": "Настройки сохранены",
    "toast.prepare_started": "Загрузка начата: {model}",
    "settings.diagnostics": "Диагностика",
    "settings.copy_diagnostics": "Скопировать сведения",
    "settings.copy_diagnostics_sub": "Версия, движок, окружение и состояние сервисов",
    "settings.dependencies": "Системные зависимости",
    "settings.deps_sub": "Внешние программы, которые нужны WayVoice. Установка запускается только по вашему запросу.",
    "settings.deps_ok": "установлено",
    "settings.deps_missing": "не хватает: {binaries} — {purpose}",
    "settings.install": "Установить",
    "settings.installing": "Установка…",
    "settings.deps_manual": "Автоустановка недоступна: установите вручную.",
    "settings.deps_no_manager": "Пакетный менеджер не найден. Установите {programs} вручную.",
    "settings.deps_no_name": "Имя пакета неизвестно для вашей системы. Установите программу вручную.",
    "settings.copy_logs": "Скопировать журнал",
    "settings.copy_logs_sub": "Последние 200 строк журнала демона — в буфер обмена",
    "shortcut.title": "Глобальная клавиша",
    "shortcut.capture": "Нажми нужное сочетание",
    "shortcut.hint": "F-клавиши можно использовать без модификаторов.",
    "shortcut.disable": "Отключить",
    "shortcut.disabled": "Отключено",
    "common.cancel": "Отмена",
    "common.delete": "Удалить",
    "size.b": "Б",
    "size.kb": "КБ",
    "size.mb": "МБ",
    "size.gb": "ГБ",
    "store.state": "Состояние модели",
    "store.storage": "Хранилище моделей",
    "store.downloaded": "скачана · {size}",
    "store.missing": "не скачана",
    "store.local": "локальная папка · {size}",
    "store.local_missing": "локальная папка не найдена",
    "store.disk": "Модели: {size} · весь кэш: {cache} · свободно {free}",
    "store.delete_title": "Удалить модель?",
    "store.delete_body": "{name} будет удалена с диска. Это можно скачать снова.",
    "store.deleting": "Удаляю…",
    "store.deleted": "Модель удалена, освобождено {size}",
    "store.deleted_shared": "Модель удалена, освобождено {freed}. Файлы на {kept} остались: их использует другая модель.",
    "store.delete_busy": "Модель загружена в распознавателе — сначала остановите его.",
    "store.delete_refused": "Не удалось удалить: {reason}",
    "store.nothing_to_delete": "Модель уже отсутствует на диске",
    "store.delete_failed": "Не удалось удалить модель: {detail}",
    "store.refuse_local": "Локальную модель удалить нельзя — она лежит вне кэша.",
    "store.refuse_missing": "Модель не скачана — на диске её нет.",
    "store.refuse_not_in_catalogue": "Этой модели нет в списке WayVoice — удалить её может только та программа, что её скачала.",
    "store.refuse_symlink": "Каталог модели является ссылкой — удаление небезопасно.",
    "store.refuse_outside": "Каталог модели находится вне кэша — удаление небезопасно.",
    "store.download": "Загрузка модели",
    "store.downloading": "Скачивается {model}",
    "store.download_progress": "{done} из {total} ({percent}%)",
    "store.download_unknown": "подключение…",
    "store.download_failed": "Загрузка не удалась",
    "store.warming": "Подготовка {model}",
    "store.warming_sub": "веса уже на диске, модель загружается в память",
    "store.download_title": "Скачать модель",
    "store.download_body": "{name} занимает {size}. Скачать сейчас?",
    "store.download_now": "Скачать",
    "store.download_later": "Позже",
    "store.download_unknown_size": "столько, сколько весит сама модель",
    "store.refuse_unknown": "Неизвестная модель.",
    "shortcut.need_modifier": "Добавь Ctrl, Alt или Super",
    "toast.custom_model": "Укажи Hugging Face repo ID или локальный путь",
    "toast.dictation_failed": "Не удалось запустить диктовку",
    "toast.restart_failed": "Не удалось восстановить фоновую службу WayVoice",
    "toast.restart_exhausted": "Автоматическое восстановление не удалось. Перезапустите WayVoice и проверьте диагностику.",
    "toast.diagnostics_copied": "Диагностика скопирована",
    "toast.diagnostics_failed": "Не удалось скопировать диагностику: {detail}",
    "toast.logs_copied": "Журнал скопирован",
    "toast.logs_failed": "Не удалось прочитать журнал: {detail}",
    "toast.logs_empty": "Журнал пуст",
    "toast.deps_installed": "Зависимость установлена",
    "toast.deps_failed": "Не удалось установить зависимость",
    "deps.missing": "Не хватает программ: {binaries}.",
    "deps.pipewire": "нужно для записи микрофона.",
    "deps.wl_clipboard": "нужно, чтобы положить распознанный текст в буфер обмена.",
    "deps.notify": "нужно для системных уведомлений о ходе диктовки.",
    "deps.ydotool": "нужно для автоматической вставки текста (работает только вместе с ydotoold).",
    "pkgsys.installed": "Установлено: {packages}.",
    "pkgsys.failed": "Ошибка установки: {reason}",
    "pkgsys.timeout": "Установка не завершилась за {seconds} с.",
    "pkgsys.timeout_detail": "Установка не завершилась за {seconds} с: {detail}",
    "pkgsys.need_root": "Для установки нужны права root, но pkexec не найден. Установите пакеты вручную через sudo.",
    "pkgsys.package_unavailable": "В репозиториях этой системы нет пакета «{package}». Установите его вручную или соберите из исходников.",
    "pkgsys.package_skipped": "Не установлено — нет пакета в репозиториях: {packages}.",
    "pkgsys.install_busy": "Другая установка уже идёт. Дождитесь её окончания.",
    "pkgsys.no_packages": "Неизвестно, какой пакет установить. Установите зависимость вручную.",
    "injector.clipboard_error": "Не удалось открыть буфер обмена: {error}",
    "injector.clipboard_write": "Не удалось записать текст в буфер обмена.",
    "injector.ydotool_missing": "Автовставка недоступна: {detail} Текст оставлен в буфере обмена.",
    "injector.ydotool_timeout": "ydotool не ответил. Текст оставлен в буфере обмена.",
    "injector.ydotool_failed": "Автовставка не сработала: {reason}. Текст оставлен в буфере обмена.",
    "injector.ydotool_failed_generic": "ydotool завершился с ошибкой",
    "injector.ydotool_helper_down": "Автовставка не сработала: помощник ydotoold не запущен. Запустите его: systemctl --user start wayvoice-ydotool.service",
    "cli.deps_unknown_id": "Неизвестная зависимость: {id}. Доступные: {ids}.",
    "cli.deps_unknown_package": "Неизвестно имя пакета для {manager}. Установите вручную: {programs}.",
    "cli.deps_no_manager": "Пакетный менеджер не найден. Установите вручную: {programs}.",
    "cli.deps_nothing_missing": "Все зависимости на месте.",
    "cli.deps_need_root": "Для установки нужны права root, но pkexec не найден. Установите пакеты вручную через sudo.",
    "cli.deps_running": "Выполняется установка: {command}",
    "cli.deps_setup_user_failed": "Пакет установлен, но горячая клавиша не настроена: {reason}",
    "cli.model_download_started": "Загрузка модели запущена.",
    "cli.model_warming": "Веса уже на диске — модель загружается в память.",
    "cli.model_download_failed": "Не удалось начать загрузку модели.",
    "cli.model_download_stopped": "Загрузка остановлена.",
    "cli.model_nothing_running": "Загрузка не выполняется.",
    "cli.model_warm_continues": "Веса уже скачаны — загрузка модели в память продолжается сама.",
    "cli.model_usage": "Использование: wayvoice model [--download|--cancel]",
    "cli.service_not_running": "Фоновый сервис WayVoice ещё не запущен.",
    "cli.service_no_answer": "Сервис WayVoice не ответил: {reason}",
    "cli.unknown_engine": "Неизвестный движок распознавания: {engine}",
    "cli.setup_started": "Запуск подготовки {engine} запрошен.",
    "cli.engine_needs_no_setup": "{engine} не требует подготовки.",
    "cli.usage": "Использование: wayvoice {commands}",
    "cli.model_choose_in_settings": "Модель выбирается в окне настроек; скачивается та, что выбрана.",
    "about.comments": 'Локальный голосовой ввод для Linux.',
    "about.developer": "WayVoice contributors",
    "ui.auto": "Системный",
    "ui.russian": "Русский",
    "ui.english": "English",
    "paste.clipboard": "Только буфер обмена",
    "paste.clipboard_short": "Буфер",
    "language.auto": "Авто",
    "daemon.model_missing": "Модель {model} ещё не скачана. Скачайте её в настройках или командой wayvoice model --download.",
    "daemon.model_prepare_busy": "Модель уже загружается.",
    "daemon.model_not_downloadable": "У этой модели нет загрузки: она не из репозитория Hub, и скачивать нечего.",
    "engine.download_unsupported": "Эту модель скачать нельзя: {model}",
    "engine.download_needs_runtime": "Рантайм Faster-Whisper не подготовлен — нечем качать модель. Сначала выполните настройку движка (в настройках или командой `wayvoice engine-setup`).",
    "engine.download_missing_helper": "Скрипт загрузки модели не найден в установке WayVoice. Переустановите пакет или сборку приложения, чтобы он появился снова.",
    "engine.download_cancelled": "Загрузка отменена",
    "engine.download_timeout": "Загрузка не закончилась за {hours} ч",
    "engine.download_missing_after": "Загрузка закончилась, но модели на диске нет",
    "daemon.busy_recognizing": "Распознавание всё ещё идёт.",
    "daemon.not_recording": "Запись не идёт.",
    "daemon.unknown_engine": "Неизвестный движок распознавания: {engine}",
    "daemon.engine_needs_no_setup": "{engine} не требует подготовки.",
    "daemon.unknown_command": "Неизвестная команда: {command}",
    "daemon.too_busy": "Демон занят.",
    "daemon.recording_started": "Запись началась",
    "daemon.recording_cancelled": "Запись отменена",
    "daemon.recording_limit": "Достигнут лимит записи ({seconds} с).",
    "daemon.transcribing": "Распознаю…",
    "daemon.no_speech": "Речь не распознана.",
    "daemon.recognition_cancelled": "Распознавание отменено.",
    "daemon.recognition_timeout": "Распознавание остановлено по тайм-ауту {seconds} с.",
    "daemon.text_inserted": "Текст вставлен",
    "diagnostics.not_available": "недоступно",
}

_EN = {
    "nav.home": "Home",
    "nav.settings": "Settings",
    "menu.update": "Check for updates…",
    "menu.about": "About WayVoice",
    "menu.diagnostics": "Copy diagnostics",
    "menu.quit": "Quit",
    "home.title": "Voice input",
    "home.subtitle": "Dictate text into any application.",
    "status.starting": "Starting…",
    "hero.starting": "Starting…",
    "hero.record": "Press and speak",
    "hero.recording": "Listening…",
    "hero.recording_hint": "Press again when you are done.",
    "hero.transcribing": "Transcribing…",
    "hero.cancel_hint": "Press to cancel transcription.",
    "hero.not_ready": "Recognition is not ready",
    "hero.open_settings": "Open Settings.",
    "hero.downloading": "Downloading {model}",
    "hero.warming": "Preparing {model}",
    "shortcut.global": "Global shortcut",
    "card.engine": "Engine",
    "card.model": "Model",
    "card.paste": "Paste",
    "health.title": "Status",
    "health.checking": "Checking…",
    "health.ready": "ready",
    "health.not_ready": "not ready",
    "health.error": "error",
    "health.warning": "warning",
    "health.deps_missing": "Missing dependencies: {names}",
    "transcript.title": "Last text",
    "transcript.empty": "Your latest dictation will appear here.",
    "transcript.last": "latest result",
    "status.recording": "Recording",
    "status.transcribing": "Transcribing",
    "status.ready": "Ready",
    "status.engine": "Engine",
    "status.preparing": "Preparing",
    "status.waiting": "waiting",
    "status.start": "Starting",
    "settings.recognition": "Recognition",
    "settings.engine": "Engine",
    "settings.engine_state": "Status",
    "settings.prepare": "Prepare",
    "settings.repair": "Repair",
    "settings.preparing": "Preparing…",
    "settings.model": "Model",
    "settings.custom_model": "Hugging Face repo or local path",
    "settings.device": "Compute device",
    "settings.vad": "Filter silence",
    "settings.worker": "Warm recognizer",
    "settings.worker_sub": "Keeps the model in memory between takes, so text appears sooner. Uses several GB of RAM.",
    "settings.parakeet_prepare": "Download CPU runtime and model (~690 MiB)",
    "settings.parakeet_binary": "Path to nemo-speech v0.2.0 (CPU)",
    "settings.parakeet_model": "Path to Parakeet v3 GGUF (automatic language detection)",
    "settings.cpp_binary": "Path to whisper-cli",
    "settings.cpp_model": "Model path",
    "settings.cpp_gpu": "Use GPU",
    "settings.custom_command": "Recognition command",
    "settings.custom_command_sub": "Runs through /bin/sh; {audio} is the path to the recording. The text is read from stdout.",
    "settings.text": "Language and text",
    "settings.language": "Recognition language",
    "settings.punctuation": "Punctuation",
    "settings.spoken": "Spoken commands",
    "settings.spoken_sub": "period, comma, new line, new paragraph",
    "settings.append_space": "Append space after paste",
    "settings.control": "Control",
    "settings.shortcut": "Global shortcut",
    "settings.change": "Change",
    "settings.paste": "Paste",
    "settings.notifications": "Notifications",
    "settings.notification_text": "Dictation text in notifications",
    "settings.notification_text_sub": "Off by default: text may remain in notification history.",
    "settings.safety": "Limits",
    "settings.timeout": "Transcription timeout",
    "settings.timeout_sub": "Stops a stuck or unexpectedly slow model run",
    "settings.max_recording": "Maximum recording",
    "settings.max_recording_sub": "Recording stops automatically and is sent for transcription",
    "settings.interface": "Interface",
    "settings.ui_language": "Interface language",
    "settings.language_pending": "Saved. Save the remaining changes to apply the interface language.",
    "settings.unsaved_title": "Unsaved changes",
    "settings.unsaved_body": "You have unsaved changes. Save them?\nModel changes apply to dictation after saving.",
    "settings.stay": "Stay",
    "settings.leave_unsaved": "Leave without saving",
    "settings.save": "Save",
    "settings.saved": "Settings saved",
    "toast.prepare_started": "Download started: {model}",
    "settings.diagnostics": "Diagnostics",
    "settings.copy_diagnostics": "Copy details",
    "settings.copy_diagnostics_sub": "Version, engine, environment and service status",
    "settings.dependencies": "System dependencies",
    "settings.deps_sub": "External programs WayVoice relies on. Nothing is installed unless you ask for it.",
    "settings.deps_ok": "installed",
    "settings.deps_missing": "missing: {binaries} — {purpose}",
    "settings.install": "Install",
    "settings.installing": "Installing…",
    "settings.deps_manual": "Automatic install unavailable: install it manually.",
    "settings.deps_no_manager": "No package manager found. Install {programs} manually.",
    "settings.deps_no_name": "Package name unknown for your system. Install the program manually.",
    "settings.copy_logs": "Copy logs",
    "settings.copy_logs_sub": "The last 200 lines of the daemon journal, into the clipboard",
    "shortcut.title": "Global shortcut",
    "shortcut.capture": "Press the shortcut you want",
    "shortcut.hint": "Function keys can be used without modifiers.",
    "shortcut.disable": "Disable",
    "shortcut.disabled": "Disabled",
    "common.cancel": "Cancel",
    "common.delete": "Delete",
    "size.b": "B",
    "size.kb": "KB",
    "size.mb": "MB",
    "size.gb": "GB",
    "store.state": "Model status",
    "store.storage": "Model storage",
    "store.downloaded": "downloaded · {size}",
    "store.missing": "not downloaded",
    "store.local": "local folder · {size}",
    "store.local_missing": "local folder not found",
    "store.disk": "Models: {size} · whole cache: {cache} · {free} free",
    "store.delete_title": "Delete the model?",
    "store.delete_body": "{name} will be removed from disk. You can download it again.",
    "store.deleting": "Deleting…",
    "store.deleted": "Model deleted, {size} freed",
    "store.deleted_shared": "Model deleted, {freed} freed. {kept} of files stayed: another model uses them.",
    "store.delete_busy": "The model is loaded in the recognizer — stop it first.",
    "store.delete_refused": "Could not delete: {reason}",
    "store.nothing_to_delete": "The model is already absent from disk",
    "store.delete_failed": "Could not delete the model: {detail}",
    "store.refuse_local": "A local model cannot be deleted — it lives outside the cache.",
    "store.refuse_missing": "The model is not downloaded — there is nothing on disk.",
    "store.refuse_not_in_catalogue": "This model is not in the WayVoice list — only the program that downloaded it can remove it.",
    "store.refuse_symlink": "The model directory is a symlink — deleting it is not safe.",
    "store.refuse_outside": "The model directory is outside the cache — deleting it is not safe.",
    "store.download": "Model download",
    "store.downloading": "Downloading {model}",
    "store.download_progress": "{done} of {total} ({percent}%)",
    "store.download_unknown": "connecting…",
    "store.download_failed": "Download failed",
    "store.warming": "Preparing {model}",
    "store.warming_sub": "The weights are on disk; the model is being loaded into memory",
    "store.download_title": "Download the model",
    "store.download_body": "{name} takes {size}. Download it now?",
    "store.download_now": "Download",
    "store.download_later": "Later",
    "store.download_unknown_size": "an unknown amount of space",
    "store.refuse_unknown": "Unknown model.",
    "shortcut.need_modifier": "Add Ctrl, Alt or Super",
    "toast.custom_model": "Enter a Hugging Face repo ID or local path",
    "toast.dictation_failed": "Could not start dictation",
    "toast.restart_failed": "Could not restore the WayVoice background service",
    "toast.restart_exhausted": "Automatic recovery failed. Restart WayVoice and check diagnostics.",
    "toast.diagnostics_copied": "Diagnostics copied",
    "toast.diagnostics_failed": "Could not copy the diagnostics: {detail}",
    "toast.logs_copied": "Logs copied",
    "toast.logs_failed": "Could not read the journal: {detail}",
    "toast.logs_empty": "The journal is empty",
    "toast.deps_installed": "Dependency installed",
    "toast.deps_failed": "Could not install the dependency",
    "deps.missing": "Missing programs: {binaries}.",
    "deps.pipewire": "needed to record the microphone.",
    "deps.wl_clipboard": "needed to put the recognized text into the clipboard.",
    "deps.notify": "needed for the desktop notifications during dictation.",
    "deps.ydotool": "needed for automatic text paste (only works together with ydotoold).",
    "pkgsys.installed": "Installed: {packages}.",
    "pkgsys.failed": "Install failed: {reason}",
    "pkgsys.timeout": "Installation did not finish within {seconds}s.",
    "pkgsys.timeout_detail": "Installation did not finish within {seconds}s: {detail}",
    "pkgsys.need_root": "Installing needs root privileges but pkexec was not found. Install the packages manually with sudo.",
    "pkgsys.package_unavailable": "This system\u2019s repositories have no \u201c{package}\u201d package. Install it by hand, or build it from source.",
    "pkgsys.package_skipped": "Not installed, no such package in the repositories: {packages}.",
    "pkgsys.install_busy": "Another installation is already running. Wait for it to finish.",
    "pkgsys.no_packages": "Unknown which package to install. Install the dependency manually.",
    "injector.clipboard_error": "Could not open the clipboard: {error}",
    "injector.clipboard_write": "Could not write the text into the clipboard.",
    "injector.ydotool_missing": "Auto-paste unavailable: {detail} The text stays in the clipboard.",
    "injector.ydotool_timeout": "ydotool did not respond. The text stays in the clipboard.",
    "injector.ydotool_failed": "Auto-paste failed: {reason}. The text stays in the clipboard.",
    "injector.ydotool_failed_generic": "ydotool exited with an error",
    "injector.ydotool_helper_down": "Auto-paste failed: the ydotoold helper is not running. Start it: systemctl --user start wayvoice-ydotool.service",
    "cli.deps_unknown_id": "Unknown dependency: {id}. Available: {ids}.",
    "cli.deps_unknown_package": "Unknown package name for {manager}. Install manually: {programs}.",
    "cli.deps_no_manager": "No package manager found. Install manually: {programs}.",
    "cli.deps_nothing_missing": "All dependencies are present.",
    "cli.deps_need_root": "Installing needs root privileges but pkexec was not found. Install the packages manually with sudo.",
    "cli.deps_running": "Running install: {command}",
    "cli.deps_setup_user_failed": "Package installed, but the hotkey was not set up: {reason}",
    "cli.model_download_started": "The model download has started.",
    "cli.model_warming": "The weights are already on disk; the model is being loaded into memory.",
    "cli.model_download_failed": "Could not start the model download.",
    "cli.model_download_stopped": "The download has been stopped.",
    "cli.model_nothing_running": "No download is running.",
    "cli.model_warm_continues": "The weights are already down; the model will finish loading into memory.",
    "cli.model_usage": "Usage: wayvoice model [--download|--cancel]",
    "cli.service_not_running": "The WayVoice background service is not running yet.",
    "cli.service_no_answer": "The WayVoice service did not answer: {reason}",
    "cli.unknown_engine": "Unknown recognition engine: {engine}",
    "cli.setup_started": "Preparing {engine} has been requested.",
    "cli.engine_needs_no_setup": "{engine} needs no preparation.",
    "cli.usage": "Usage: wayvoice {commands}",
    "cli.model_choose_in_settings": "The model is chosen in the settings window; the chosen one is downloaded.",
    "about.comments": 'Local voice dictation for Linux.',
    "about.developer": "WayVoice contributors",
    "ui.auto": "System",
    "ui.russian": "Русский",
    "ui.english": "English",
    "paste.clipboard": "Clipboard only",
    "paste.clipboard_short": "Clipboard",
    "language.auto": "Auto",
    "daemon.model_missing": "The model {model} is not downloaded yet. Download it in the settings, or with wayvoice model --download.",
    "daemon.model_prepare_busy": "The model is already being downloaded.",
    "daemon.model_not_downloadable": "This model has no download: it is not a Hub repository, so there is nothing to fetch.",
    "engine.download_unsupported": "This model cannot be downloaded: {model}",
    "engine.download_needs_runtime": "The Faster-Whisper runtime is not prepared, so there is nothing to fetch the model with. Run the engine setup first (Settings or `wayvoice engine-setup`).",
    "engine.download_missing_helper": "The model download script is missing from the WayVoice install. Reinstall the package or application build to restore it.",
    "engine.download_cancelled": "The download was cancelled",
    "engine.download_timeout": "The download did not finish within {hours} hours",
    "engine.download_missing_after": "The download finished but the model is not on disk",
    "daemon.busy_recognizing": "Recognition is still running.",
    "daemon.not_recording": "Recording is not active.",
    "daemon.unknown_engine": "Unknown recognition engine: {engine}",
    "daemon.engine_needs_no_setup": "{engine} needs no preparation.",
    "daemon.unknown_command": "Unknown command: {command}",
    "daemon.too_busy": "The daemon is busy.",
    "daemon.recording_started": "Recording started",
    "daemon.recording_cancelled": "Recording cancelled",
    "daemon.recording_limit": "Recording limit reached ({seconds}s).",
    "daemon.transcribing": "Transcribing…",
    "daemon.no_speech": "No speech was recognized.",
    "daemon.recognition_cancelled": "Recognition cancelled.",
    "daemon.recognition_timeout": "Recognition stopped after {seconds}s timeout.",
    "daemon.text_inserted": "Text inserted",
    "diagnostics.not_available": "unavailable",
}

_RU.update({'store.download_recovery': 'Проверьте соединение и свободное место, затем повторите загрузку. Подробности доступны ниже.',
 'settings.engine_needs_setup': 'Подготовьте движок или повторите его установку.',
 'settings.engine_settings_required': 'Проверьте настройки выбранного движка.',
 'transcript.copying': 'Копирование…', 'transcript.clearing': 'Очистка…',
 'common.close': 'Закрыть',
 'store.delete_held_hint': 'Модель в памяти. При удалении она будет безопасно выгружена.',
 'settings.draft_hint': 'Изменения применяются после сохранения. Загрузка файлов и подготовка движка '
                        'выполняются отдельно.',
 'settings.save_dirty': 'Сохранить изменения',
 'menu.close_settings': 'Закрыть настройки',
 'home.background_note': 'Закрытие окна не останавливает WayVoice. Запись остановите клавишей или кнопкой.',
 'home.button_clipboard': 'Кнопка сохраняет текст в буфере. Перейдите в приложение и вставьте его.',
 'home.shortcut_insert': 'Для вставки в другое приложение используйте настроенную горячую клавишу.',
 'health.attention': 'Нужна помощь',
 'health.daemon_unavailable': 'Служба голосового ввода недоступна.',
 'health.retry_hint': 'Повторите запуск или откройте диагностику.',
 'health.retry': 'Повторить запуск',
 'health.retrying': 'Повторный запуск…',
 'health.details': 'Технические подробности',
 'health.diagnostics': 'Диагностика',
 'health.recorder_error': 'Не удалось записать звук.',
 'health.recorder_hint': 'Проверьте микрофон и PipeWire, затем повторите запись.',
 'health.timeout_error': 'Распознавание не завершилось вовремя.',
 'health.timeout_hint': 'Повторите с более короткой записью или увеличьте таймаут в настройках.',
 'health.clipboard_error': 'Не удалось доставить текст.',
 'health.clipboard_hint': 'Скопируйте последний текст и проверьте способ вставки в настройках.',
 'health.config_error': 'Настройки не удалось прочитать.',
 'health.config_hint': 'Проверьте файл настроек; подробности доступны ниже.',
 'health.backend_error': 'Операция не завершена.',
 'health.backend_hint': 'Повторите операцию. Если ошибка повторится, сохраните диагностику.',
 'hero.ready_hint': 'Готово к записи.',
 'mic.record': 'Начать запись в буфер',
 'mic.stop': 'Остановить запись',
 'mic.cancel': 'Отменить распознавание',
 'mic.preparing': 'Модель подготавливается',
 'mic.unavailable': 'Запись недоступна',
 'transcript.copy': 'Копировать',
 'transcript.clear': 'Очистить',
 'transcript.copied': 'Текст скопирован',
 'transcript.clear_failed': 'Не удалось очистить текст: {detail}',
 'transcript.copy_failed': 'Не удалось скопировать текст: {detail}',
 'store.stop_and_delete': 'Освободить и удалить',
 'store.delete_stop_body': 'Модель «{name}» будет выгружена из памяти и удалена. Во время диктовки удаление '
                           'недоступно.',
 'store.cancel_pending': 'Отменяется…',
 'store.cancel_failed': 'Не удалось отменить: {detail}',
 'store.cancel_requested': 'Отмена запрошена',
 'store.cancel_stale': 'Операция уже изменилась. Проверьте её текущее состояние.',
 'store.maintenance_busy': 'Сначала завершите запись, распознавание или подготовку модели.',
 'store.worker_stop_failed': 'Не удалось безопасно выгрузить модель. Удаление отменено.',
 'shortcut.manual_required': 'Автоматическая регистрация здесь недоступна. Назначьте клавишу для команды: '
                             '{command}',
 'shortcut.backend_unavailable': 'Служба горячих клавиш недоступна. Настройте вручную: {command}',
 'shortcut.gnome_configured': 'Клавиша настроена. Проверьте её из другого приложения.',
 'shortcut.apply_failed': 'Клавиша не настроена: {error}. Ручная команда: {command}',
 'diagnostics.preview_title': 'Проверить диагностику',
 'diagnostics.preview_notice': 'Отчёт содержит сведения о системе, движке и настроенных путях. Журналы могут '
                               'содержать ошибки. Проверьте данные перед отправкой. Копирование заменит '
                               'текст диктовки в буфере.',
 'diagnostics.copy': 'Копировать отчёт',
 'diagnostics.save': 'Сохранить отчёт',
 'diagnostics.saved': 'Отчёт сохранён',
 'diagnostics.save_failed': 'Не удалось сохранить отчёт: {detail}',
 'diagnostics.log_missing': 'Журнал прямого запуска ещё недоступен: {path}'})
_EN.update({'store.download_recovery': 'Check the connection and free disk space, then retry. Details are available below.',
 'settings.engine_needs_setup': 'Prepare the engine or retry its installation.',
 'settings.engine_settings_required': 'Check the selected engine settings.',
 'transcript.copying': 'Copying…', 'transcript.clearing': 'Clearing…',
 'common.close': 'Close',
 'store.delete_held_hint': 'The model is in memory. Deletion will safely unload it.',
 'settings.draft_hint': 'Changes apply after saving. File downloads and engine preparation are separate '
                        'operations.',
 'settings.save_dirty': 'Save changes',
 'menu.close_settings': 'Close settings',
 'home.background_note': 'Closing this window keeps WayVoice running. Stop recording with the shortcut or '
                         'button.',
 'home.button_clipboard': 'This button copies text to the clipboard. Switch to your application and paste '
                          'it.',
 'home.shortcut_insert': 'Use the configured shortcut to insert into another application.',
 'health.attention': 'Needs attention',
 'health.daemon_unavailable': 'The voice-input service is unavailable.',
 'health.retry_hint': 'Retry startup or open diagnostics.',
 'health.retry': 'Retry startup',
 'health.retrying': 'Retrying startup…',
 'health.details': 'Technical details',
 'health.diagnostics': 'Diagnostics',
 'health.recorder_error': 'Audio recording failed.',
 'health.recorder_hint': 'Check your microphone and PipeWire, then record again.',
 'health.timeout_error': 'Recognition timed out.',
 'health.timeout_hint': 'Try a shorter recording or increase the timeout in settings.',
 'health.clipboard_error': 'Text delivery failed.',
 'health.clipboard_hint': 'Copy the last text and check the paste method in settings.',
 'health.config_error': 'Settings could not be read.',
 'health.config_hint': 'Check the settings file; details are available below.',
 'health.backend_error': 'The operation could not be completed.',
 'health.backend_hint': 'Retry the operation. If it fails again, save diagnostics.',
 'hero.ready_hint': 'Ready to record.',
 'mic.record': 'Start recording to clipboard',
 'mic.stop': 'Stop recording',
 'mic.cancel': 'Cancel recognition',
 'mic.preparing': 'Model preparation in progress',
 'mic.unavailable': 'Recording unavailable',
 'transcript.copy': 'Copy',
 'transcript.clear': 'Clear',
 'transcript.copied': 'Text copied',
 'transcript.clear_failed': 'Could not clear text: {detail}',
 'transcript.copy_failed': 'Could not copy text: {detail}',
 'store.stop_and_delete': 'Unload and delete',
 'store.delete_stop_body': 'Model “{name}” will be unloaded and deleted. Deletion is unavailable during '
                           'dictation.',
 'store.cancel_pending': 'Cancelling…',
 'store.cancel_failed': 'Could not cancel: {detail}',
 'store.cancel_requested': 'Cancellation requested',
 'store.cancel_stale': 'The operation has changed. Check its current state.',
 'store.maintenance_busy': 'Finish recording, recognition or model preparation first.',
 'store.worker_stop_failed': 'The model could not be safely unloaded. Deletion was cancelled.',
 'shortcut.manual_required': 'Automatic registration is unavailable here. Bind a shortcut to: {command}',
 'shortcut.backend_unavailable': 'The shortcut service is unavailable. Configure manually: {command}',
 'shortcut.gnome_configured': 'Shortcut configured. Test it from another application.',
 'shortcut.apply_failed': 'Shortcut setup failed: {error}. Manual command: {command}',
 'diagnostics.preview_title': 'Review diagnostics',
 'diagnostics.preview_notice': 'The report contains system, engine and configured-path details. Logs may '
                               'include errors. Review before sharing. Copy replaces the dictation '
                               'clipboard.',
 'diagnostics.copy': 'Copy report',
 'diagnostics.save': 'Save report',
 'diagnostics.saved': 'Report saved',
 'diagnostics.save_failed': 'Could not save report: {detail}',
 'diagnostics.log_missing': 'No direct-process log is available yet: {path}'})

_RU.update({'setup.title': 'Перед первой диктовкой',
 'setup.description': 'Это проверка подготовки, а не проверка микрофона или вставки. Настройки '
                      'применяются кнопкой «Сохранить».',
 'setup.configure': 'Настроить',
 'setup.capture': 'Запись звука',
 'setup.waiting': 'Ожидаем связь с WayVoice',
 'setup.runtime_ready': 'Движок настроен; качество проверьте первой записью',
 'setup.runtime_missing': 'Нужно подготовить или настроить движок',
 'setup.model_ready': 'Файлы выбранной модели доступны',
 'setup.model_missing': 'Модель не скачана',
 'setup.model_pending': 'Модель скачивается или готовится',
 'setup.model_external': 'Файлы задаются в настройках движка; их работоспособность ещё не '
                         'проверена',
 'setup.capture_missing': 'Не найдена программа записи — проверьте зависимости',
 'setup.capture_available': 'Программа записи найдена; микрофон ещё не проверен',
 'setup.clipboard_missing': 'Не найден wl-copy — установите wl-clipboard в зависимостях',
 'setup.clipboard_available': 'wl-copy найден; вставку в нужное приложение проверьте горячей '
                              'клавишей',
 'setup.shortcut_disabled': 'Горячая клавиша отключена',
 'setup.shortcut_configured': 'Настройка доступна; сохраните клавишу и проверьте её в другом приложении',
 'setup.shortcut_manual': 'Требуется ручная привязка команды в окружении рабочего стола',
 'setup.model_next': 'Откройте шаг «Модель» ниже и скачайте файлы.',
 'menu.help': 'Помощь и обновления',
 'help.title': 'Первый диктант и обновления',
 'help.steps': '1. Подготовьте движок и файлы модели в настройках.\n'
               '2. Сохраните изменения и настройте горячую клавишу.\n'
               '3. Перейдите в текстовое поле другого приложения. Нажмите клавишу, скажите фразу и '
               'нажмите ещё раз.\n'
               '4. Если автовставка недоступна, вставьте текст из буфера вручную. Кнопка в окне '
               'WayVoice всегда использует буфер.',
 'help.native': 'Для пакетной установки используйте обновления вашего дистрибутива. Если '
                'устанавливали .deb из GitHub, скачайте новую версию на странице выпусков и '
                'установите её тем же способом.',
 'help.flatpak': 'Обновите WayVoice через магазин приложений или flatpak update. Для локально '
                 'установленного bundle скачайте новую сборку из того же источника.',
 'help.releases': 'Выпуски WayVoice',
 'help.issues': 'Сообщить об ошибке'})
_EN.update({'setup.title': 'Before your first dictation',
 'setup.description': 'Preparation checks, not a microphone or paste test. Apply settings with '
                      'Save.',
 'setup.configure': 'Configure',
 'setup.capture': 'Audio recording',
 'setup.waiting': 'Waiting for WayVoice',
 'setup.runtime_ready': 'Engine configured; verify it with your first recording',
 'setup.runtime_missing': 'Prepare or configure the engine',
 'setup.model_ready': 'Selected model files available',
 'setup.model_missing': 'Model not downloaded',
 'setup.model_pending': 'Model downloading or preparing',
 'setup.model_external': 'Files are configured in engine settings; their operation is unverified',
 'setup.capture_missing': 'Recording program missing — check dependencies',
 'setup.capture_available': 'Recording program found; microphone unverified',
 'setup.clipboard_missing': 'wl-copy missing — install wl-clipboard in dependencies',
 'setup.clipboard_available': 'wl-copy found; test pasting into your application with the shortcut',
 'setup.shortcut_disabled': 'Shortcut disabled',
 'setup.shortcut_configured': 'Configuration is available; save the shortcut and test it in another application',
 'setup.shortcut_manual': 'Bind the command manually in your desktop settings',
 'setup.model_next': 'Open the Model step below and download the files.',
 'menu.help': 'Help and updates',
 'help.title': 'First dictation and updates',
 'help.steps': '1. Prepare the engine and model files in Settings.\n'
               '2. Save changes and configure the shortcut.\n'
               '3. Focus a text field in another application. Press the shortcut, speak, then '
               'press again.\n'
               '4. If automatic paste is unavailable, paste from the clipboard manually. The '
               'WayVoice window button always uses the clipboard.',
 'help.native': 'For a packaged installation, use your distribution updates. If you installed a '
                'GitHub .deb, download a new version from Releases and install it in the same way.',
 'help.flatpak': 'Update WayVoice through your application store or flatpak update. For a locally '
                 'installed bundle, obtain the new build from the same source.',
 'help.releases': 'WayVoice releases',
 'help.issues': 'Report a problem'})

_RU.update({'profiles.title': 'Простой выбор распознавания',
 'profiles.description': 'Локальный Faster-Whisper на CPU, без требования видеокарты. Меняются '
                         'только движок, модель и вычислитель в черновике. Выбор ничего не '
                         'скачивает. Сохранение может начать подготовку движка; файлы модели '
                         'скачиваются отдельно. Скорость и качество зависят от речи и компьютера.',
 'profiles.fast': 'Быстро',
 'profiles.fast_detail': 'Tiny · самая лёгкая многоязычная модель; возможны ошибки на сложной '
                         'речи.',
 'profiles.balanced': 'Сбалансированно',
 'profiles.balanced_detail': 'Small · больше ресурсов, чем Tiny; баланс скорости и точности.',
 'profiles.accurate': 'Точнее',
 'profiles.accurate_detail': 'Medium · больше памяти и времени, чем Small; для сложной речи.',
 'profiles.choose': 'Выбрать',
 'profiles.selected': 'Пресет выбран. При наличии изменений нажмите «Сохранить». Файлы модели '
                      'скачиваются отдельно.'})
_EN.update({'profiles.title': 'Simple recognition choices',
 'profiles.description': 'Local Faster-Whisper on CPU; no GPU required. Only engine, model and '
                         'compute device change in the draft. Choosing a preset downloads nothing. '
                         'Saving may start engine setup; model files are downloaded separately. '
                         'Speed and quality depend on speech and hardware.',
 'profiles.fast': 'Fast',
 'profiles.fast_detail': 'Tiny · lightest multilingual model; may struggle with complex speech.',
 'profiles.balanced': 'Balanced',
 'profiles.balanced_detail': 'Small · more resources than Tiny; balances speed and accuracy.',
 'profiles.accurate': 'More accurate',
 'profiles.accurate_detail': 'Medium · more memory and time than Small; for complex speech.',
 'profiles.choose': 'Choose',
 'profiles.selected': 'Preset selected. Save if there are changes. Model files are downloaded '
                      'separately.'})

_RU.update({"settings.saved_warning": "Настройки сохранены. Для подготовки или клавиши нужны дополнительные действия; причина доступна на главной."})
_EN.update({"settings.saved_warning": "Settings saved. Setup or shortcut needs attention; details are available on Home."})

_RU.update({'indicator.title': 'WayVoice — индикатор',
 'indicator.open': 'Открыть индикатор',
 'indicator.limit': 'Обычное окно состояния. Размещение и перекрытие управляются рабочим столом. '
                    'Закрытие окна не останавливает WayVoice.',
 'indicator.ready': 'Используйте настроенную горячую клавишу в нужном приложении.',
 'indicator.processing': 'Распознаётся запись. Для отмены откройте главную страницу.',
 'indicator.preparing': 'Идёт загрузка или подготовка модели.'})
_EN.update({'indicator.title': 'WayVoice — indicator',
 'indicator.open': 'Open indicator',
 'indicator.limit': 'A normal status window. Placement and occlusion are controlled by your '
                    'desktop. Closing it does not stop WayVoice.',
 'indicator.ready': 'Use your configured shortcut in the target application.',
 'indicator.processing': 'Recognizing the recording. Open Home to cancel.',
 'indicator.preparing': 'Downloading or preparing a model.'})

_RU.update({"indicator.open_app": "Открыть WayVoice"})
_EN.update({"indicator.open_app": "Open WayVoice"})

_RU.update({"shortcut.kde_manual": "В KDE Plasma назначьте клавишу в Параметрах системы → Клавиатура → Комбинации клавиш → Добавить → Команда или сценарий. Команда: {command}. Выбор клавиши в WayVoice сохраняет желаемую комбинацию, но не регистрирует её в Plasma."})
_EN.update({"shortcut.kde_manual": "In KDE Plasma, bind a key in System Settings → Keyboard → Shortcuts → Add New → Command or Script. Command: {command}. Choosing a key in WayVoice saves the desired binding but does not register it in Plasma."})

_RU.update({'onboard.step': 'Шаг {number} из 4',
 'onboard.welcome': 'Добро пожаловать в WayVoice',
 'onboard.welcome_detail': 'Говорите — текст появится в вашем приложении. Распознавание работает '
                           'на компьютере: записи не отправляются в облако.',
 'onboard.language': 'Язык интерфейса',
 'onboard.system': 'Как в системе',
 'onboard.language_detail': 'Язык речи определяется автоматически. Его можно изменить отдельно в '
                            'настройках.',
 'onboard.continue': 'Продолжить',
 'onboard.back': 'Назад',
 'onboard.models': 'Выберите модель',
 'onboard.models_detail': 'Все три модели распознают разные языки и работают на CPU. Чем больше '
                          'модель, тем больше нужно памяти и времени. Выбор можно изменить позже.',
 'onboard.tiny': 'Быстро · Tiny',
 'onboard.tiny_detail': 'Самая лёгкая модель. Для простых фраз; чаще ошибается на сложной речи.',
 'onboard.small': 'Баланс · Small',
 'onboard.small_detail': 'Рекомендуем для начала: баланс скорости и точности.',
 'onboard.medium': 'Точнее · Medium',
 'onboard.medium_detail': 'Для сложной речи. Требует больше памяти и работает медленнее.',
 'onboard.no_download': 'Выбор модели ничего не скачивает.',
 'onboard.prepare': 'Подготовим распознавание',
 'onboard.preparing': 'Подготовка распознавания',
 'onboard.network': 'По вашей команде WayVoice скачает недостающие компоненты и выбранную модель. '
                    'Нужен интернет. После подготовки диктовка работает локально.',
 'onboard.size': 'Модель: примерно {size}. Компоненты скачиваются дополнительно.',
 'onboard.confirm': 'Начать сейчас или вернуться к этому позже?',
 'onboard.runtime': 'Подготовка компонентов…',
 'onboard.download': 'Загрузка модели…',
 'onboard.install': 'Скачать и подготовить',
 'onboard.close_detail': 'Отмена остановит эту загрузку. Подготовка компонентов, запущенная ранее, '
                         'может продолжиться.',
 'onboard.cancel': 'Отмена',
 'onboard.defer': 'Настроить позже',
 'onboard.ready': 'Модель готова',
 'onboard.later': 'Продолжим позже',
 'onboard.ready_detail': 'Модель скачана. Осталось проверить горячую клавишу и попробовать первую '
                         'диктовку.',
 'onboard.later_detail': 'Выбор сохранится. Диктовка станет доступна после подготовки компонентов '
                         'и загрузки модели в настройках.',
 'onboard.shortcut': 'Проверьте и настройте горячую клавишу в настройках WayVoice.',
 'onboard.clipboard': 'Если автоматическая вставка недоступна, распознанный текст останется в '
                      'буфере обмена для ручной вставки.',
 'onboard.open': 'Открыть WayVoice',
 'onboard.cancelled': 'Подготовка отменена. Можно повторить попытку или настроить позже.',
 'onboard.cancelling': 'Отмена…',
 'onboard.failed': 'Не удалось завершить: {reason}'})
_EN.update({'onboard.step': 'Step {number} of 4',
 'onboard.welcome': 'Welcome to WayVoice',
 'onboard.welcome_detail': 'Speak and text appears in your application. Recognition runs on your '
                           'computer; recordings are not sent to the cloud.',
 'onboard.language': 'Interface language',
 'onboard.system': 'System default',
 'onboard.language_detail': 'Speech language is detected automatically. You can change it '
                            'separately in settings.',
 'onboard.continue': 'Continue',
 'onboard.back': 'Back',
 'onboard.models': 'Choose a model',
 'onboard.models_detail': 'All three models support multiple languages and run on CPU. Larger '
                          'models need more memory and time. You can change your choice later.',
 'onboard.tiny': 'Fast · Tiny',
 'onboard.tiny_detail': 'The lightest model. For simple phrases; less reliable with complex '
                        'speech.',
 'onboard.small': 'Balanced · Small',
 'onboard.small_detail': 'Recommended to start: a balance of speed and accuracy.',
 'onboard.medium': 'More accurate · Medium',
 'onboard.medium_detail': 'For complex speech. Uses more memory and takes longer.',
 'onboard.no_download': 'Selecting a model does not download anything.',
 'onboard.prepare': 'Prepare recognition',
 'onboard.preparing': 'Preparing recognition',
 'onboard.network': 'When you start, WayVoice downloads missing components and your selected '
                    'model. Internet access is required. Afterwards, dictation works locally.',
 'onboard.size': 'Model: about {size}. Components are downloaded separately.',
 'onboard.confirm': 'Start now or come back later?',
 'onboard.runtime': 'Preparing components…',
 'onboard.download': 'Downloading model…',
 'onboard.install': 'Download and prepare',
 'onboard.close_detail': 'Cancel stops this download. Component preparation started earlier may '
                         'continue.',
 'onboard.cancel': 'Cancel',
 'onboard.defer': 'Set up later',
 'onboard.ready': 'Model ready',
 'onboard.later': 'Continue later',
 'onboard.ready_detail': 'Your model is downloaded. Check your shortcut, then try your first '
                         'dictation.',
 'onboard.later_detail': 'Your choice will be saved. Dictation becomes available after preparing '
                         'components and downloading the model in settings.',
 'onboard.shortcut': 'Check and configure your shortcut in WayVoice settings.',
 'onboard.clipboard': 'If automatic paste is unavailable, recognized text stays in the clipboard '
                      'for you to paste.',
 'onboard.open': 'Open WayVoice',
 'onboard.cancelled': 'Preparation cancelled. Retry or set up later.',
 'onboard.cancelling': 'Cancelling…',
 'onboard.failed': 'Could not complete: {reason}'})

_RU.update({'onboard.kde_shortcut': 'В настройках WayVoice нажмите «Настроить в KDE» и выберите горячую клавишу в системном окне Plasma.',
 'indicator.window_actions': 'Действия окна',
 'indicator.window_actions_hint': 'Меню рабочего стола: выберите «Поверх остальных». '
                                  'Перетаскивайте индикатор за состояние.',
 'indicator.details': 'Подробности',
 'indicator.window_actions_unavailable': 'Меню окна недоступно. В KDE используйте Alt+F3 → '
                                         'Дополнительные действия → Поверх остальных.',
 'shortcut.portal.configure': 'Настроить в KDE',
 'shortcut.portal.choose': 'Выберите горячую клавишу в настройках KDE Plasma.',
 'shortcut.portal.unavailable': 'Системная настройка клавиши недоступна. Можно назначить команду '
                                'WayVoice вручную.',
 'shortcut.portal.hint': 'Нажмите «Настроить в KDE» и назначьте клавишу в открывшихся системных '
                         'настройках. WayVoice покажет фактическую комбинацию; она работает, пока '
                         'запущен daemon, даже при закрытом окне.',
 'shortcut.portal.active': 'Назначено в KDE: {binding}'})
_EN.update({'onboard.kde_shortcut': 'In WayVoice settings, click Configure in KDE and choose a shortcut in Plasma desktop settings.',
 'indicator.window_actions': 'Window actions',
 'indicator.window_actions_hint': 'Desktop window menu: choose Keep Above. Drag the indicator by '
                                  'its status.',
 'indicator.details': 'Details',
 'indicator.window_actions_unavailable': 'Window menu unavailable. In KDE use Alt+F3 → More '
                                         'Actions → Keep Above.',
 'shortcut.portal.configure': 'Configure in KDE',
 'shortcut.portal.choose': 'Choose a shortcut in KDE Plasma settings.',
 'shortcut.portal.unavailable': 'Desktop shortcut configuration is unavailable. You can bind the '
                                'WayVoice command manually.',
 'shortcut.portal.hint': 'Click Configure in KDE and assign a key in the desktop settings. '
                         'WayVoice shows the actual binding; it works while the daemon runs, even '
                         'with this window closed.',
 'shortcut.portal.active': 'Assigned in KDE: {binding}'})

_RU.update({'unit.seconds': '{value} с',
 'unit.minutes': '{value} мин',
 'update.title': 'Обновление WayVoice',
 'update.checking': 'Проверка GitHub Releases…',
 'update.available': 'Установлена {installed}. Доступна {available}.\n'
                     'Установка откроется в терминале и потребует подтверждения. Завершите '
                     'диктовку; службы будут перезапущены. После обновления закройте и откройте '
                     'настройки.',
 'update.install': 'Установить…',
 'update.current': 'Установлена актуальная версия: {version}',
 'update.launcher_missing': 'Не найден запускатель wayvoice',
 'model.subtitle.tiny': 'Самая лёгкая · многоязычная',
 'model.subtitle.base': 'Быстрая · многоязычная',
 'model.subtitle.small': 'Баланс скорости и точности',
 'model.subtitle.medium': 'Точнее, но тяжелее',
 'model.subtitle.large-v3': 'Максимальная точность · многоязычная',
 'model.subtitle.turbo': 'Большая модель с упором на скорость',
 'model.subtitle.tiny.en': 'Английская · очень лёгкая',
 'model.subtitle.base.en': 'Английская · быстрая',
 'model.subtitle.small.en': 'Английская · хороший баланс',
 'model.subtitle.medium.en': 'Английская · высокая точность',
 'model.subtitle.distil-large-v3': 'Английская · ускоренная distilled-модель',
 'model.subtitle.bzikst/faster-whisper-large-v3-russian-int8': 'Дообучена на русской речи · '
                                                               'community',
 'model.subtitle.tnfru/whisper-large-v3-german-ct2': 'Дообучена на немецкой речи · community',
 'model.subtitle.nekusu/faster-whisper-large-v3-turbo-latam-int8-ct2': 'Латиноамериканский '
                                                                       'испанский · community',
 'model.subtitle.ele-sage/whisper-large-v3-turbo-fr-quebecois-ct2': 'Французский Квебека · '
                                                                    'community',
 'model.subtitle.LocalAI-io/whisper-large-v3-it-yodas-only-ct2-int8': 'Дообучена на итальянской '
                                                                      'речи · community',
 'model.subtitle.__custom__': 'Hugging Face repo ID или локальный путь'})
_EN.update({'unit.seconds': '{value} s',
 'unit.minutes': '{value} min',
 'update.title': 'WayVoice update',
 'update.checking': 'Checking GitHub Releases…',
 'update.available': 'Installed: {installed}. Available: {available}.\n'
                     'Installation opens in a terminal and asks for confirmation. Finish '
                     'dictation; services will restart. Reopen settings afterwards.',
 'update.install': 'Install…',
 'update.current': 'Up to date: {version}',
 'update.launcher_missing': 'wayvoice launcher unavailable',
 'model.subtitle.tiny': 'Lightest · multilingual',
 'model.subtitle.base': 'Fast · multilingual',
 'model.subtitle.small': 'Balanced speed and accuracy',
 'model.subtitle.medium': 'More accurate, heavier',
 'model.subtitle.large-v3': 'Highest accuracy · multilingual',
 'model.subtitle.turbo': 'Large model optimized for speed',
 'model.subtitle.tiny.en': 'English-only · very light',
 'model.subtitle.base.en': 'English-only · fast',
 'model.subtitle.small.en': 'English-only · balanced',
 'model.subtitle.medium.en': 'English-only · high accuracy',
 'model.subtitle.distil-large-v3': 'English-only · distilled and faster',
 'model.subtitle.bzikst/faster-whisper-large-v3-russian-int8': 'Fine-tuned for Russian · '
                                                               'community',
 'model.subtitle.tnfru/whisper-large-v3-german-ct2': 'Fine-tuned for German · community',
 'model.subtitle.nekusu/faster-whisper-large-v3-turbo-latam-int8-ct2': 'Latin American Spanish · '
                                                                       'community',
 'model.subtitle.ele-sage/whisper-large-v3-turbo-fr-quebecois-ct2': 'Québec French · community',
 'model.subtitle.LocalAI-io/whisper-large-v3-it-yodas-only-ct2-int8': 'Fine-tuned for Italian · '
                                                                      'community',
 'model.subtitle.__custom__': 'Hugging Face repo ID or local path'})

_RU.update({'shortcut.mode': 'Режим горячей клавиши',
 'shortcut.mode_toggle': 'Нажать для начала и остановки',
 'shortcut.mode_hold': 'Удерживать для записи (PTT)',
 'shortcut.hold_hint': 'Удерживайте клавишу во время записи. Отпустите для распознавания. Кнопка '
                       'микрофона работает по нажатию.',
 'shortcut.hold_unavailable': 'Текущий способ привязки не передаёт отпускание клавиши. Для PTT '
                              'нужны системные команды на нажатие и отпускание.'})
_EN.update({'shortcut.mode': 'Shortcut mode',
 'shortcut.mode_toggle': 'Press to start and stop',
 'shortcut.mode_hold': 'Hold to talk (PTT)',
 'shortcut.hold_hint': 'Hold the shortcut while speaking. Release to transcribe. The microphone '
                       'button remains click-to-toggle.',
 'shortcut.hold_unavailable': 'This shortcut backend does not report key release. PTT requires '
                              'desktop commands for both press and release.'})

_RU.update({'shortcut.hold_recording': 'Отпустите горячую клавишу для распознавания.'})
_EN.update({'shortcut.hold_recording': 'Release the shortcut to transcribe.'})

_RU.update({'update.auto': 'Автоматически проверять обновления',
 'update.auto_hint': 'Проверять GitHub при открытии и раз в 6 часов, пока интерфейс открыт. Без '
                     'автоматического скачивания и установки.',
 'update.notice': 'Доступна WayVoice {version}',
 'update.open': 'Обновить…'})
_EN.update({'update.auto': 'Automatically check for updates',
 'update.auto_hint': 'Check GitHub on opening and every 6 hours while the interface is open. No '
                     'automatic downloads or installation.',
 'update.notice': 'WayVoice {version} is available',
 'update.open': 'Update…'})

_TRANSLATIONS = {"ru": _RU, "en": _EN}

_RU.update({'update.external': 'Обновите эту установку через пакетный менеджер или Flatpak. Обновление в '
                    'приложении доступно для установленного DEB или RPM WayVoice.',
 'update.busy': 'Дождитесь завершения текущей операции.',
 'update.interrupted': 'Процесс обновления завершился без подтверждения результата.',
 'update.restart_failed': 'Пакет установлен, но перезапуск служб не удался.',
 'update.stage.check': 'Проверка обновлений…',
 'update.stage.download': 'Загрузка обновления…',
 'update.stage.verify': 'Проверка пакета…',
 'update.stage.authorize': 'Ожидание прав администратора и установка…',
 'update.stage.install': 'Установка обновления…',
 'update.stage.restart': 'Перезапуск служб…',
 'update.stage.reopen': 'Перезапуск WayVoice…',
 'update.available': 'Установлена {installed}. Доступна {available}.\n'
                     'После установки WayVoice и службы перезапустятся. Завершите диктовку и '
                     'сохраните настройки.'})
_EN.update({'update.external': 'Update this installation through your package manager or Flatpak. In-app '
                    'installation requires an installed WayVoice DEB or RPM.',
 'update.busy': 'Please wait for the current operation to finish.',
 'update.interrupted': 'The updater ended without confirming completion.',
 'update.restart_failed': 'The package was installed, but service restart failed.',
 'update.stage.check': 'Checking for updates…',
 'update.stage.download': 'Downloading the update…',
 'update.stage.verify': 'Verifying the package…',
 'update.stage.authorize': 'Waiting for administrator authorization and installing…',
 'update.stage.install': 'Installing the update…',
 'update.stage.restart': 'Restarting services…',
 'update.stage.reopen': 'Restarting WayVoice…',
 'update.available': 'Installed: {installed}. Available: {available}.\n'
                     'WayVoice and its services will restart after installation. Finish dictation '
                     'and save your settings first.'})


_RU.update({'daemon.text_copied': 'Текст скопирован в буфер обмена',
 'update.failed': 'Не удалось завершить обновление. Посмотрите подробности или повторите попытку.',
 'update.retry': 'Повторить'})
_EN.update({'daemon.text_copied': 'Text copied to clipboard',
 'update.failed': 'The update could not be completed. See technical details or try again.',
 'update.retry': 'Try again'})


_RU.update({'audio.title': 'Звук',
 'audio.hint': 'Смена микрофона применяется после сохранения. Обновите список, чтобы найти '
               'подключённые устройства.',
 'audio.device': 'Микрофон',
 'audio.default': 'По умолчанию в системе',
 'audio.missing': 'Недоступен: {name}',
 'audio.refresh': 'Обновить список микрофонов',
 'audio.check': 'Проверить микрофон',
 'audio.check_hint': 'Короткая запись выполняется только по запросу. Звук не сохраняется.',
 'audio.listening': 'Слушаю… Скажите что-нибудь.',
 'audio.silent': 'Звук не обнаружен. Проверьте устройство и отключение микрофона.',
 'audio.detected': 'Звук обнаружен. Полоса показывает пиковый уровень записи.',
 'audio.cues': 'Звуковые сигналы записи',
 'audio.cues_hint': 'Короткие сигналы при начале, завершении и отмене записи.'})
_EN.update({'audio.title': 'Audio',
 'audio.hint': 'Microphone changes apply after saving. Refresh the list to find connected '
               'devices.',
 'audio.device': 'Microphone',
 'audio.default': 'System default',
 'audio.missing': 'Unavailable: {name}',
 'audio.refresh': 'Refresh microphones',
 'audio.check': 'Check microphone',
 'audio.check_hint': 'Records a short sample only when requested. Audio is not saved.',
 'audio.listening': 'Listening… Speak now.',
 'audio.silent': 'No sound detected. Check the device and mute switch.',
 'audio.detected': 'Sound detected. The bar shows the sample peak.',
 'audio.cues': 'Recording sounds',
 'audio.cues_hint': 'Short sounds when recording starts, stops or is cancelled.'})

_RU.update({'vocab.title': 'Личный словарь',
 'vocab.enabled': 'Применять замены текста',
 'vocab.hint': 'Одна замена на строку: распознанная фраза => нужный текст. Фразы заменяются '
               'целиком, без учёта регистра; приоритет у более длинных фраз. Замены '
               'применяются после пунктуации, без повторной обработки результата.',
 'vocab.example': 'Например: вей войс => WayVoice',
 'vocab.preview': 'Предпросмотр',
 'vocab.sample': 'Пример текста',
 'vocab.result': 'Результат предпросмотра',
 'vocab.empty': 'Предпросмотр ещё не выполнен.',
 'vocab.invalid': 'Проверьте правила словаря перед сохранением.',
 'vocab.limit': 'Не более 100 замен и 16 384 символов.',
 'vocab.off': 'Замены отключены. Предпросмотр использует текущие настройки черновика.'})
_EN.update({'vocab.title': 'Personal dictionary',
 'vocab.enabled': 'Apply text replacements',
 'vocab.hint': 'One replacement per line: recognized phrase => preferred text. Whole phrases, '
               'ignoring case; longer phrases win. Replacements apply after punctuation, '
               'without chaining.',
 'vocab.example': 'e.g. way voice => WayVoice',
 'vocab.preview': 'Preview',
 'vocab.sample': 'Sample text',
 'vocab.result': 'Preview result',
 'vocab.empty': 'No preview yet.',
 'vocab.invalid': 'Check dictionary rules before saving.',
 'vocab.limit': 'Maximum 100 replacements and 16,384 characters.',
 'vocab.off': 'Replacements are off. Preview uses the current draft settings.'})

for _code in UI_LANGUAGE_NAMES:
    if _code not in _TRANSLATIONS:
        _TRANSLATIONS[_code] = json.loads(
            files("wayvoice").joinpath("locales", _code + ".json").read_text(encoding="utf-8")
        )


def ui_language_labels(language: str | None = None) -> list[str]:
    """Keep language names readable even when the current UI language is unfamiliar."""
    return [tr("ui.auto", language), *UI_LANGUAGE_NAMES.values()]



def resolve_language(value: str | None) -> str:
    """Resolve an explicit or system UI locale independently of speech language."""
    value = value.strip().lower() if isinstance(value, str) else languages.AUTO
    normalized = languages.normalize(value)
    if normalized in _TRANSLATIONS:
        return normalized
    message_locale = (os.environ.get('LC_ALL') or os.environ.get('LC_MESSAGES')
                      or os.environ.get('LANG') or '')
    if message_locale.split('.', 1)[0].upper() in {'C', 'POSIX'}:
        return 'en'
    candidates = os.environ.get('LANGUAGE', '').split(':') + [message_locale]
    candidates.append(_locale_language(lambda: locale.getlocale(locale.LC_MESSAGES)))
    for candidate in candidates:
        detected = languages.detect_from_locale([candidate])
        if detected in _TRANSLATIONS:
            return detected
    return 'en'


def _locale_language(getter) -> str:
    """The language part of what a locale getter returned, if it returned one."""
    if getter is None:
        return ""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            result = getter()
    except Exception:
        return ""
    if isinstance(result, tuple):
        result = result[0] if result else ""
    return str(result or "")


def tr(key: str, language: str | None = None, **kwargs: Any) -> str:
    lang = resolve_language(language)
    value = _TRANSLATIONS.get(lang, _EN).get(key, _EN.get(key, key))
    if kwargs:
        try:
            return value.format(**kwargs)
        except Exception:
            return value
    return value

_RU["update.external"] = "Обновите эту установку вручную. Для AppImage завершите WayVoice и замените файл. Обновление в приложении доступно только для установленного DEB или RPM."
_EN["update.external"] = "Update this installation manually. For AppImage, quit WayVoice and replace the file. In-app installation requires an installed DEB or RPM."

# Native navigation and microphone-level labels.
_TRANSLATIONS_09 = {'en': {'menu.main': 'Main Menu', 'audio.level': 'Microphone Level'},
                    'ru': {'menu.main': 'Главное меню', 'audio.level': 'Уровень микрофона'}}
for _language, _labels in _TRANSLATIONS_09.items():
    _TRANSLATIONS[_language].update(_labels)
