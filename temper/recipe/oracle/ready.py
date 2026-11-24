"""Print 1 once the subject's database answers a query, else 0."""
import sys

import psycopg

from hermitage import HOSTS

try:
    with psycopg.connect(**HOSTS[sys.argv[1]], connect_timeout=3) as c:
        c.execute("SELECT 1")
    print(1)
except Exception:
    print(0)
