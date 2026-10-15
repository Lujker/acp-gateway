"""Actionable error cards; diagnostics remain redacted and no work is retried."""

# ruff: noqa: RUF001
from acp_gateway.agents import (
    AgentUnavailable,
    AuthenticationFailed,
    ModeNotAvailable,
    SessionBusy,
    SessionNotFound,
    TLSFingerprintMismatch,
    TransportDisconnected,
)
from acp_gateway.agents.errors import ModelRefused
from acp_gateway.core import (
    JobNotFound,
    MaintenanceBlocked,
    RateLimited,
    SelectionChanged,
    UnknownAgent,
    UnknownSession,
)
from acp_gateway.log import redact_text


def error_card(error):
    if isinstance(error, ModelRefused):
        text = "Агент отклонил смену модели. Проверьте сохранённые сведения и доступные модели."
        rows = [[("Сведения о модели", "/model state"), ("Модели", "/models")]]
    elif isinstance(error, ModeNotAvailable):
        text = (
            "Агент не поддерживает настроенный режим. "
            "Проверьте профиль или выберите другого агента."
        )
        rows = [[("Выбрать агента", "/agent"), ("Компьютеры", "/computers")]]
    elif isinstance(error, (AuthenticationFailed, TLSFingerprintMismatch)):
        text = "Подключение отклонено. Проверьте настройки доступа и доверия на компьютере."
        rows = [[("Компьютеры", "/computers"), ("Выбрать агента", "/agent")]]
    elif isinstance(error, TransportDisconnected):
        text = (
            "Связь с агентом прервана. Проверьте задачу в истории. "
            "Не отправляйте запрос повторно, пока его исход неизвестен."
        )
        rows = [[("История задач", "/jobs"), ("Компьютеры", "/computers")]]
    elif isinstance(error, AgentUnavailable):
        text = "Агент недоступен. Проверьте подключение компьютера или выберите другого агента."
        rows = [[("Компьютеры", "/computers"), ("Выбрать агента", "/agent")]]
    elif isinstance(error, UnknownAgent):
        text = "Агент не выбран или больше недоступен. Откройте список доступных агентов."
        rows = [[("Выбрать агента", "/agent"), ("Компьютеры", "/computers")]]
    elif isinstance(error, (UnknownSession, SessionNotFound)):
        text = "Диалог не выбран или недоступен. Откройте список диалогов либо историю задач."
        rows = [[("Диалоги", "/sessions"), ("История задач", "/jobs")]]
    elif isinstance(error, SessionBusy):
        text = (
            "Предыдущая операция ещё выполняется или требует проверки. "
            "Проверьте активные задачи и выбранный диалог."
        )
        rows = [[("Активные задачи", "/jobs active"), ("Диалоги", "/sessions")]]
    elif isinstance(error, JobNotFound):
        text = "Задача недоступна этому чату. Обновите историю и выберите задачу из списка."
        rows = [[("История задач", "/jobs")]]
    elif isinstance(error, RateLimited):
        text = (
            f"Слишком много запросов. Подождите {error.retry_after} с и проверьте активные задачи."
        )
        rows = [[("Активные задачи", "/jobs active")]]
    elif isinstance(error, MaintenanceBlocked):
        text = "Диспетчер временно не принимает эту операцию. Проверьте его состояние."
        rows = [[("Статус", "/status")]]
    elif isinstance(error, SelectionChanged):
        text = "Выбор агента или диалога изменился. Обновите панель перед следующим действием."
        rows = [[("Обновить панель", "/menu")]]
    else:
        text = "Действие не выполнено. Проверьте пояснение и откройте нужный раздел панели."
        rows = []
    detail = redact_text(str(error))[:1000]
    return f"{text}\n\nПояснение: {detail}", rows
