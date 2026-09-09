"""Einstiegspunkt fuer gunicorn: `gunicorn terminal_app:app`."""

from terminal.server import app  # noqa: F401

if __name__ == "__main__":
    from terminal.server import main
    main()
