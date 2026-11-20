from personal_cinema.runtime_paths import root_path as _cinema_path, endpoint as _cinema_endpoint, python_path as _cinema_python
from .clients import clientManager

class UserInterface(object):

    def __init__(self):
        self.open_player_menu = lambda: None
        self.stop = lambda: None

    @staticmethod
    def login_servers():
        clientManager.cli_connect()

    def start(self):
        pass

    def stop(self):
        pass
user_interface = UserInterface()
