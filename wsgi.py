"""WSGI entry point for production deployment.

Usage:
    # Gunicorn (Linux / Mac)
    gunicorn wsgi:app -b 0.0.0.0:7860 -w 4

    # Waitress (Windows)
    waitress-serve --port=7860 wsgi:app

    # uWSGI
    uwsgi --http 0.0.0.0:7860 --wsgi-file wsgi.py --callable app

The Flask development server (``python app.py``) still works for local use.
"""

from app import app

# Many WSGI servers look for ``application`` by convention
application = app
