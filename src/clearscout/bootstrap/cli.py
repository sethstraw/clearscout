"""The ClearScout command-line application."""

from clearscout.delivery.cli.auth import auth_app
from clearscout.delivery.cli.database import db_app
from clearscout.delivery.cli.root import register_root_commands
from clearscout.delivery.cli.search import search_app
from clearscout.delivery.cli.support import app, terminal
from clearscout.delivery.cli.tasks import tasks_app

app.add_typer(auth_app, name="auth")
app.add_typer(db_app, name="db")
app.add_typer(search_app, name="search")
app.add_typer(tasks_app, name="tasks")
register_root_commands()

__all__ = ["app", "terminal"]
