from __future__ import annotations

from app.services.site_parser import ParsedPost
from app.services.xhs.models import XhsNote


class XhsLikesAdapter:
    @staticmethod
    def to_post(note: XhsNote) -> ParsedPost:
        # No expiring media URL or worker reference enters the generic parser pipeline.
        return ParsedPost(url=note.url, title=note.title or note.note_id,
                          pub_date=note.published_at, tags=[], assets=[])
