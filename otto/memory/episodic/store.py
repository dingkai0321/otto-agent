"""Episodic memory — dated events in PostgreSQL."""

from __future__ import annotations


class PostgresEpisodeStore:
    def __init__(self, conn):
        self.conn = conn

    def add(self, summary: str, happened_at: str) -> None:
        self.conn.execute(
            "INSERT INTO episodes (happened_at, summary) VALUES (%s, %s)",
            (happened_at, summary),
        )
        self.conn.commit()

    def search(self, query: str, top_k: int = 3) -> list[str]:
        query = query.strip()
        if not query:
            return self.recent(top_k)
        pattern = f"%{query}%"
        rows = self.conn.execute(
            """SELECT happened_at, summary
                 FROM episodes
                WHERE search_vector @@ plainto_tsquery('simple', %s)
                   OR summary ILIKE %s
                ORDER BY ts_rank_cd(search_vector, plainto_tsquery('simple', %s)) DESC,
                         happened_at DESC
                LIMIT %s""",
            (query, pattern, query, top_k),
        ).fetchall()
        return [f"({r['happened_at']}) {r['summary']}" for r in rows]

    def recent(self, top_k: int = 3) -> list[str]:
        rows = self.conn.execute(
            "SELECT happened_at, summary FROM episodes ORDER BY happened_at DESC LIMIT %s",
            (top_k,),
        ).fetchall()
        return [f"({r['happened_at']}) {r['summary']}" for r in rows]

    def list(self, limit: int = 200) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, happened_at, summary, created_at FROM episodes "
            "ORDER BY id DESC LIMIT %s",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def delete(self, episode_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM episodes WHERE id=%s", (episode_id,))
        self.conn.commit()
        return cur.rowcount > 0
