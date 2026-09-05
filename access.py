from __future__ import annotations


def is_authorized(user_id: int | None, admin_ids: tuple[int, ...]) -> bool:
    return user_id is not None and user_id in admin_ids
