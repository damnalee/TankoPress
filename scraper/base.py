from abc import ABC, abstractmethod
from typing import Any


class BaseMangaProvider(ABC):
    @abstractmethod
    async def get_manga_info(self, url: str) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    async def get_chapter_list(self, url: str) -> list[dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    async def get_chapter_images(self, chapter_url: str) -> list[str]:
        raise NotImplementedError
