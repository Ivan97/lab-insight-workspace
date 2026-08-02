from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parents[2]
load_dotenv(ROOT_DIR / ".env", override=False)
DATA_DIR = ROOT_DIR / "data"
DATABASE_PATH = DATA_DIR / "app.duckdb"
ARTIFACT_DIR = DATA_DIR / "artifacts"
DEMO_SOURCE_DIR = ROOT_DIR / "demo_data" / "raw"
CATALOG_ID = "https://mini-hackathon.local/a2ui/catalogs/analytics-chat/v1"
CATALOG_PATH = ROOT_DIR / "contracts" / "a2ui" / "catalogs" / "analytics-chat-v1.json"
FRONTEND_DIST = ROOT_DIR / "frontend" / "dist"

DATA_DIR.mkdir(parents=True, exist_ok=True)
ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
DEMO_SOURCE_DIR.mkdir(parents=True, exist_ok=True)
