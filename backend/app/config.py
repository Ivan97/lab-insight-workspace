from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT_DIR / "data"
DATABASE_PATH = DATA_DIR / "app.duckdb"
ARTIFACT_DIR = DATA_DIR / "artifacts"
CATALOG_ID = "https://mini-hackathon.local/a2ui/catalogs/analytics-chat/v1"
CATALOG_PATH = ROOT_DIR / "contracts" / "a2ui" / "catalogs" / "analytics-chat-v1.json"
FRONTEND_DIST = ROOT_DIR / "frontend" / "dist"

DATA_DIR.mkdir(parents=True, exist_ok=True)
ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
