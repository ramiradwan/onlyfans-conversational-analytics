"""Read-only compatibility facade over canonical persisted conversations."""

from __future__ import annotations

from app.analytics.pipeline import CanonicalReadModelSource
from app.models.core import ChatThread, Message, UserRef


class OnlyFansClient:
    """Expose legacy chat models without maintaining a second data cache."""

    def __init__(self, repository: CanonicalReadModelSource) -> None:
        self.repository = repository

    async def get_chats(
        self,
        creator_account_id: str,
        limit: int = 20,
        offset: int = 0,
    ) -> list[ChatThread]:
        account = self.repository.account_read_model(creator_account_id)
        chats = [
            self._chat(conversation)
            for _, conversation in sorted(account.conversations.items())
        ]
        return chats[offset : offset + limit]

    async def get_messages(
        self,
        creator_account_id: str,
        chat_id: int | str,
        limit: int = 20,
        offset: int = 0,
    ) -> list[Message]:
        account = self.repository.account_read_model(creator_account_id)
        conversation = account.conversations.get(str(chat_id))
        if conversation is None:
            return []
        messages = [
            self._message(str(chat_id), item)
            for item in conversation.get("messages", [])
        ]
        return messages[offset : offset + limit]

    async def get_chat_with_messages(
        self,
        creator_account_id: str,
        chat_id: int | str,
        message_limit: int = 20,
    ) -> ChatThread:
        account = self.repository.account_read_model(creator_account_id)
        conversation = account.conversations.get(str(chat_id))
        if conversation is None:
            raise ValueError(f"Chat {chat_id} not found.")
        chat = self._chat(conversation)
        chat.messages = [
            self._message(str(chat_id), item)
            for item in conversation.get("messages", [])[:message_limit]
        ]
        return chat

    @staticmethod
    def _chat(conversation: dict) -> ChatThread:
        return ChatThread(
            id=conversation["conversation_id"],
            withUser=UserRef(
                id=conversation["platform_user_id"],
                displayName=conversation.get("display_name"),
            ),
            unread_count=conversation.get("unread_count", 0),
            messages=[
                OnlyFansClient._message(conversation["conversation_id"], item)
                for item in conversation.get("messages", [])
            ],
        )

    @staticmethod
    def _message(chat_id: str, message: dict) -> Message:
        return Message(
            id=message["message_id"],
            chat_id=chat_id,
            text=message["text"],
            created_at=message["sent_at"],
            is_inbound=message["direction"] == "inbound",
        )
