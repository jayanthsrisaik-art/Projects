"""FastAPI service. DB path from LOGPULSE_DB (default in-memory)."""
import os

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .detect import detect
from .store import connect, ingest


class IngestBody(BaseModel):
    lines: list[str]


def create_app(db_path: str | None = None) -> FastAPI:
    app = FastAPI(title="logpulse")
    con = connect(db_path or os.environ.get("LOGPULSE_DB", ":memory:"))

    @app.post("/ingest")
    def post_ingest(body: IngestBody):
        return ingest(con, body.lines)

    @app.get("/templates")
    def templates(limit: int = 10):
        rows = con.execute(
            "SELECT t.template, COUNT(*) AS count FROM logs l JOIN templates t USING(template_id) "
            "GROUP BY t.template_id ORDER BY count DESC, t.template LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    @app.get("/anomalies")
    def anomalies(train_fraction: float = 0.6, method: str = "iforest"):
        if not 0.1 <= train_fraction <= 0.9 or method not in ("iforest", "zscore"):
            raise HTTPException(422, "train_fraction must be in [0.1, 0.9]; method iforest|zscore")
        n = con.execute("SELECT COUNT(DISTINCT ts / 60) FROM logs").fetchone()[0]
        if n < 20:
            raise HTTPException(409, "need at least 20 minute-windows of data")
        d = detect(con, train_fraction=train_fraction, method=method)
        return {"split_window": int(d.split_win),
                "flagged": [{"window": int(w), "score": round(float(s), 4)}
                            for w, s, f in zip(d.windows, d.scores, d.flagged) if f]}

    return app
