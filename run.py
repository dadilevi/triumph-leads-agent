"""Start the local app:  python run.py   →  http://localhost:5050"""
from app import create_app
from app.config import Config

if __name__ == "__main__":
    cfg = Config.from_env()
    # 127.0.0.1 only: lead data never leaves the Mac
    create_app(cfg).run(host="127.0.0.1", port=cfg.port, debug=False)
