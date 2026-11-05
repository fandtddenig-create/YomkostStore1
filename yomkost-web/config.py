import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
BASE_DIR = Path(__file__).resolve().parent
SEED_DEMO = os.getenv("SEED_DEMO", "true").lower() != "false"


def _normalize(url: str):
    """Приймає будь-який URL від Neon, Supabase або Render і готує його для SQLAlchemy."""
    url = (url or "").strip()
    if not url:
        return f"sqlite+aiosqlite:///{BASE_DIR / 'data' / 'shop.db'}", {}
    if url.startswith("sqlite"):
        return url, {"connect_args": {"timeout": 30}}
    for old in ("postgres://", "postgresql://", "postgresql+psycopg2://"):
        if url.startswith(old):
            url = "postgresql+asyncpg://" + url[len(old):]
            break
    base, _, query = url.partition("?")
    keep, ssl = [], False
    for part in query.split("&") if query else []:
        key = part.split("=")[0]
        if key in ("sslmode", "ssl"):
            ssl = ssl or part.split("=")[-1] not in ("disable", "false")
        elif key != "channel_binding":
            keep.append(part)
    url = base + ("?" + "&".join(keep) if keep else "")
    args = {"statement_cache_size": 0}
    if ssl or "localhost" not in url and "127.0.0.1" not in url:
        args["ssl"] = True
    return url, {"connect_args": args, "pool_recycle": 280}


DATABASE_URL, ENGINE_ARGS = _normalize(os.getenv("DATABASE_URL", ""))
IS_SQLITE = DATABASE_URL.startswith("sqlite")
SQLITE_PATH = Path(DATABASE_URL.split("///", 1)[1]) if IS_SQLITE else None
