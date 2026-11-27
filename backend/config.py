import os
from pathlib import Path
from dotenv import load_dotenv

# 自动定位项目根目录 (his-tag-kg/)
BASE_DIR = Path(__file__).resolve().parent.parent
BACKEND_DIR = BASE_DIR / "backend"
STORAGE_DIR = BACKEND_DIR / "storage"
CACHE_DIR = STORAGE_DIR / "raw_cache"
SAMPLE_DATA_DIR = BASE_DIR / "sample_data"

# 确保必要目录存在
STORAGE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)
SAMPLE_DATA_DIR.mkdir(parents=True, exist_ok=True)

# 数据库文件路径
DB_PATH = STORAGE_DIR / "kg_database.sqlite"

# 加载环境变量
load_dotenv(BASE_DIR / ".env")


class Settings:
    LLM_API_KEY: str = os.getenv("LLM_API_KEY", "")
    LLM_BASE_URL: str = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
    LLM_MODEL: str = os.getenv("LLM_MODEL", "gpt-4o-mini")
    OPENALEX_EMAIL: str = os.getenv("OPENALEX_EMAIL", "kg-builder@example.com")
    REQUEST_TIMEOUT: float = 25.0


settings = Settings()