"""The TailorCV command-line application."""

from tailorcv.delivery.cli.auth import auth_app
from tailorcv.delivery.cli.database import db_app
from tailorcv.delivery.cli.root import register_root_commands
from tailorcv.delivery.cli.search import search_app
from tailorcv.delivery.cli.support import app, terminal
from tailorcv.delivery.cli.tasks import tasks_app

app.add_typer(auth_app, name="auth")
app.add_typer(db_app, name="db")
app.add_typer(search_app, name="search")
app.add_typer(tasks_app, name="tasks")
register_root_commands()

__all__ = ["app", "terminal"]
