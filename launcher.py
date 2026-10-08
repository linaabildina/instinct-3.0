# PyInstaller dependency anchors.
# main.py imports several modules dynamically from the GUI, so keep explicit
# imports here to make them visible even when building with "pyinstaller launcher.py".
from app import storage as _storage
import app.main as _main
from app import ai as _ai
from app import config as _config
from app import reminders as _reminders
from app import knowledge as _knowledge
from app import knowledge_ui as _knowledge_ui
from app import source_sync as _source_sync
from app import game_features as _game_features
from app import forum_search as _forum_search
from app import youtube_monitor as _youtube_monitor
from app import web_search as _web_search

from app.gui import App

if __name__ == "__main__":
    App().mainloop()
