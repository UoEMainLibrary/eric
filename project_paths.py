from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent
DATA_DIR = ROOT_DIR / "data"
INPUT_DIR = DATA_DIR / "input"
OUTPUT_DIR = DATA_DIR / "output"
LOG_DIR = DATA_DIR / "logs"
DB_DIR = DATA_DIR / "db"

TINYURLS_CSV = INPUT_DIR / "tinyurls.csv"
RECENT_ITEMS_CSV = OUTPUT_DIR / "recent_items.csv"
RECENT_TINYURLS_CSV = OUTPUT_DIR / "recent_item_tinyurls.csv"
MISSING_LUNA_CSV = OUTPUT_DIR / "missing_luna_records.csv"
CHECKPOINT_JSON = OUTPUT_DIR / "get_data_checkpoint.json"
ERIC_INGEST_CSV = OUTPUT_DIR / "eric_ingest.csv"
ERIC_DB = DB_DIR / "eric.db"


def ensure_project_dirs():
    for path in (INPUT_DIR, OUTPUT_DIR, LOG_DIR, DB_DIR):
        path.mkdir(parents=True, exist_ok=True)
