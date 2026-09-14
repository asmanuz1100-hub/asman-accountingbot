from sqlalchemy import text

from database import engine, init_db


RESET_VERSION = "clean-start-2026-09-14-v1"


def reset_business_data_once() -> bool:
    """Delete all user/business data once, while keeping schema and bot code.

    The marker lives in a tiny technical table so Render restarts do not wipe
    newly-entered data again after the first successful clean start.
    """
    init_db()

    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS app_meta ("
                "meta_key VARCHAR(128) PRIMARY KEY, "
                "meta_value VARCHAR(255) NOT NULL"
                ")"
            )
        )
        done = conn.execute(
            text("SELECT meta_value FROM app_meta WHERE meta_key = :key"),
            {"key": "business_data_reset"},
        ).scalar()
        if done == RESET_VERSION:
            return False

        if engine.dialect.name == "postgresql":
            conn.execute(
                text(
                    "TRUNCATE TABLE contracts, materials, documents, partners "
                    "RESTART IDENTITY CASCADE"
                )
            )
        else:
            # Foreign-key-safe order for SQLite/local runs.
            conn.execute(text("DELETE FROM contracts"))
            conn.execute(text("DELETE FROM materials"))
            conn.execute(text("DELETE FROM documents"))
            conn.execute(text("DELETE FROM partners"))
            try:
                conn.execute(
                    text(
                        "DELETE FROM sqlite_sequence "
                        "WHERE name IN ('contracts','materials','documents','partners')"
                    )
                )
            except Exception:
                pass

        conn.execute(
            text(
                "INSERT INTO app_meta (meta_key, meta_value) VALUES (:key, :value) "
                "ON CONFLICT (meta_key) DO UPDATE SET meta_value = EXCLUDED.meta_value"
            )
            if engine.dialect.name == "postgresql"
            else text(
                "INSERT OR REPLACE INTO app_meta (meta_key, meta_value) VALUES (:key, :value)"
            ),
            {"key": "business_data_reset", "value": RESET_VERSION},
        )

    return True
