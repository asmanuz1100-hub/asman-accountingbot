from sqlalchemy import text
from database import engine, init_db

PURGE_ID = "xisob-approved-clean-20260920-v1"

def purge_once():
    init_db()
    if engine.dialect.name != "postgresql":
        raise RuntimeError("This cleanup is only for PostgreSQL")
    with engine.begin() as conn:
        if conn.execute(text("SELECT current_database()")).scalar_one() != "asman_accounting_db_v2":
            raise RuntimeError("Wrong database, stopped")
        conn.execute(text("CREATE TABLE IF NOT EXISTS public.app_meta (meta_key VARCHAR(128) PRIMARY KEY, meta_value VARCHAR(255) NOT NULL)"))
        done = conn.execute(text("SELECT meta_value FROM public.app_meta WHERE meta_key = 'xisob_purge_20260920'")).scalar_one_or_none()
        if done == PURGE_ID:
            return False
        conn.execute(text("TRUNCATE TABLE public.contracts, public.materials, public.documents, public.partners RESTART IDENTITY RESTRICT"))
        conn.execute(text("INSERT INTO public.app_meta(meta_key, meta_value) VALUES ('xisob_purge_20260920',:v) ON CONFLICT (meta_key) DO UPDATE SET meta_value=EXCLUDED.meta_value"), {"v":PURGE_ID})
    return True

if __name__ == "__main__":
    print("Cleaned" if purge_once() else "Already cleaned")
