"""Запуск бота через Python проекта для менеджеров процессов вроде XUSS run."""

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
PYTHON = ROOT / ".venv" / "bin" / "python"

if __name__ == "__main__":
    if not PYTHON.is_file():
        raise SystemExit(f"Не найден Python окружения: {PYTHON}")
    os.chdir(ROOT)
    os.environ["PYTHONUNBUFFERED"] = "1"
    os.environ["PATH"] = str(PYTHON.parent) + os.pathsep + os.environ.get("PATH", "")
    os.execv(str(PYTHON), [str(PYTHON), str(ROOT / "bot.py"), *sys.argv[1:]])
