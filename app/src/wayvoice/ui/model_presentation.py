"""Model status text and home presentation; no filesystem or subprocess work."""
from .. import model_store
from ..models import display_name


def model_state_text(ctx, entry: dict) -> tuple[str, bool]:
    """Subtitle for the selected model, and whether it may be deleted.

    A local path is never deletable: those files belong to the user and WayVoice did not
    put them there.
    """
    kind = str(entry.get("kind") or "")
    size = model_store.human_size(int(entry.get("size_bytes") or 0), ctx.state.ui_lang)
    if kind == "local":
        if entry.get("downloaded"):
            return ctx.state.t("store.local", size=size), False
        return ctx.state.t("store.local_missing"), False
    if entry.get("downloaded"):
        return ctx.state.t("store.downloaded", size=size), True
    return ctx.state.t("store.missing"), False


def hero_preparation_caption(ctx, model_report) -> tuple[str, str, str] | None:
    """What the main window should say about a model being fetched or loaded.

    Reads the same report the settings window paints its download row from.
    Returns ``None`` when the hero has nothing preparation-shaped to say: no
    report or an
    error - an error belongs to the settings row and the toast, and quoting
    it in the hero would repeat it every 650 ms.
    """
    report = model_report if isinstance(model_report, dict) else {}
    download = report.get("download") if isinstance(report.get("download"), dict) else {}
    state = str(download.get("state") or "idle")
    # The daemon reports the load into memory inside "downloading" with
    # ``warming`` set: the weights are down and the wait is no longer a
    # transfer, which the settings row already words differently.
    if state == "downloading" and download.get("warming"):
        state = "warming"
    if state not in {"downloading", "warming"}:
        return None
    model_id = str(download.get("model") or "")
    if not model_id:
        return None
    if state == "warming":
        return (
            ctx.state.t("hero.warming", model=display_name(model_id)),
            ctx.state.t("store.warming_sub"),
            "folder-download-symbolic",
        )
    done = int(download.get("done_bytes") or 0)
    total = int(download.get("total_bytes") or 0)
    if total > 0 and done > 0:
        caption = ctx.state.t(
            "store.download_progress",
            done=model_store.human_size(done, ctx.state.ui_lang),
            total=model_store.human_size(total, ctx.state.ui_lang),
            percent=int(min(100, done * 100 / total)),
        )
    elif total > 0:
        caption = ctx.state.t("store.download_unknown")
    else:
        caption = ctx.state.t("store.download_unknown")
    return (
        ctx.state.t("hero.downloading", model=display_name(model_id)),
        caption,
        "folder-download-symbolic",
    )


def show_hero_preparation(ctx, title: str, caption: str, icon: str) -> None:
    """Paint the hero from a preparation report, pill included."""
    ctx.home.status_pill.set_text(ctx.state.t("status.preparing"))
    ctx.home.hero_state.set_text(title)
    ctx.home.hero_caption.set_text(caption)
    ctx.home.mic_icon.set_from_icon_name(icon)


def show_model_error_details(ctx, error):
    """Keep backend wording selectable, separate from localized recovery guidance."""
    if hasattr(ctx.settings, 'model_error_detail'):
        ctx.settings.model_error_detail.set_text(str(error or ''))
        ctx.settings.model_error_expander.set_visible(bool(error))
    elif hasattr(getattr(ctx.settings, 'model_download_row', None), 'set_tooltip_text'):
        ctx.settings.model_download_row.set_tooltip_text(str(error or ''))
