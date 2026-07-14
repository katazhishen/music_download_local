"""WSGI entry point for production deployment.

Usage:
    # Gunicorn (Linux / Mac)
    gunicorn wsgi:app -b 0.0.0.0:5000 -w 4

    # Waitress (Windows)
    waitress-serve --port=5000 wsgi:app

    # uWSGI
    uwsgi --http 0.0.0.0:5000 --wsgi-file wsgi.py --callable app

The Flask development server (``python web.py``) still works for local use.
"""

from web import app

# Many WSGI servers look for ``application`` by convention
application = app
