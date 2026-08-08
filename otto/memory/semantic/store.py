"""Semantic memory — durable facts searched with PostgreSQL full-text search."""

from __future__ import annotations


class PostgresFactStore:
    def __init__(self, conn):
        self.conn = conn

    def add(self, subject: str, content: str, source: str = "user") -> None:
        self.conn.execute(
            "INSERT INTO facts (subject, content, source) VALUES (%s, %s, %s)",
            (subject.lower().strip(), content, source),
        )
        self.conn.commit()

    def search(self, query: str, top_k: int = 4) -> list[str]:
        query = query.strip()
        if not query:
            return []
        pattern = f"%{query}%"
        rows = self.conn.execute(
            """SELECT subject, content
                 FROM facts
                WHERE search_vector @@ plainto_tsquery('simple', %s)
                   OR subject ILIKE %s OR content ILIKE %s
                ORDER BY ts_rank_cd(search_vector, plainto_tsquery('simple', %s)) DESC,
                         created_at DESC
                LIMIT %s""",
            (query, pattern, pattern, query, top_k),
        ).fetchall()
        return [f"[{r['subject']}] {r['content']}" for r in rows]

    def list(self, limit: int = 200) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, subject, content, source, created_at FROM facts "
            "ORDER BY id DESC LIMIT %s",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def search_with_ids(self, query: str, top_k: int = 8) -> list[dict]:
        query = query.strip()
        if not query:
            return self.list(top_k)
        pattern = f"%{query}%"
        rows = self.conn.execute(
            """SELECT id, subject, content
                 FROM facts
                WHERE search_vector @@ plainto_tsquery('simple', %s)
                   OR subject ILIKE %s OR content ILIKE %s
                ORDER BY ts_rank_cd(search_vector, plainto_tsquery('simple', %s)) DESC,
                         created_at DESC
                LIMIT %s""",
            (query, pattern, pattern, query, top_k),
        ).fetchall()
        return [dict(r) for r in rows]

    def update(self, fact_id: int, content: str, subject: str | None = None) -> bool:
        if subject is None:
            cur = self.conn.execute("UPDATE facts SET content=%s WHERE id=%s", (content, fact_id))
        else:
            cur = self.conn.execute(
                "UPDATE facts SET content=%s, subject=%s WHERE id=%s",
                (content, subject.lower().strip(), fact_id),
            )
        self.conn.commit()
        return cur.rowcount > 0

    def delete(self, fact_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM facts WHERE id=%s", (fact_id,))
        self.conn.commit()
        return cur.rowcount > 0
