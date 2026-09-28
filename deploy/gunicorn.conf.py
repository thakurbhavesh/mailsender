"""Gunicorn config — production WSGI server.

Run:  gunicorn -c deploy/gunicorn.conf.py core.wsgi:application
"""
import multiprocessing
import os

bind = os.environ.get('GUNICORN_BIND', '127.0.0.1:8001')
workers = int(os.environ.get('GUNICORN_WORKERS', max(2, multiprocessing.cpu_count() * 2 + 1)))
worker_class = 'sync'
threads = int(os.environ.get('GUNICORN_THREADS', 2))
timeout = 120
graceful_timeout = 30
keepalive = 5
max_requests = 1000        # restart workers after N requests (prevents memory leaks)
max_requests_jitter = 100  # random jitter to stagger restarts
accesslog = '-'            # stdout (captured by systemd)
errorlog = '-'
loglevel = 'info'
preload_app = True         # forks faster; uses less memory
