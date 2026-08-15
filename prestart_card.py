from __future__ import annotations


MAX_DESCRIPTION_LENGTH = 512
DEFAULT_PRESTART_DESCRIPTION = (
    "Здесь можно отправить сообщение в подключённую предложку. "
    "Нажмите Start, выберите нужный канал и режим приватности."
)
SUPPORTED_MEDIA_TYPES = frozenset({"photo", "video", "animation"})
BOTFATHER_URL = "https://t.me/BotFather"



def validate_description(text: str) -> str:
    if not isinstance(text, str):
        raise ValueError("Description must be text")
    normalized = text.strip()
    if not normalized:
        raise ValueError("Description is empty")
    if len(normalized) > MAX_DESCRIPTION_LENGTH:
        raise ValueError("Description is too long")
    return normalized


def validate_media(media_type: str | None, file_id: str | None) -> tuple[str | None, str | None]:
    if media_type is None and file_id is None:
        return None, None
    if media_type not in SUPPORTED_MEDIA_TYPES or not isinstance(file_id, str) or not file_id.strip():
        raise ValueError("Unsupported pre-start media")
    return media_type, file_id.strip()


async def apply_description(bot, text: str) -> str:
    normalized = validate_description(text)
    await bot.set_my_description(description=normalized)
    return normalized


def description_picture_apply_instructions(media_type: str) -> str:
    if media_type not in SUPPORTED_MEDIA_TYPES:
        raise ValueError("Unsupported pre-start media")
    media_label = {
        "photo": "изображение",
        "video": "видео",
        "animation": "анимацию/GIF",
    }[media_type]
    return (
        "Медиа подготовлено. Telegram пока не даёт Bot API-метод для изменения "
        "Description Picture этой карточки. Откройте @BotFather → выберите этого бота → "
        "Edit Bot → Edit Description Picture и отправьте подготовленное " + media_label + "."
    )


def description_picture_remove_instructions() -> str:
    return (
        "Локальная настройка медиа очищена. Если Description Picture уже было применено в Telegram, "
        "удалите его через @BotFather → выберите этого бота → Edit Bot → Edit Description Picture."
    )
