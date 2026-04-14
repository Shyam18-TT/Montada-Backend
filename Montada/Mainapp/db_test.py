import time
from django.db import connection

def test_db_latency(iterations=5):
    print("\n🔍 DB LATENCY TEST START\n")

    # ---- 1. Query round-trip time ----
    query_times = []

    for i in range(iterations):
        start = time.time()

        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()

        end = time.time()
        t = end - start
        query_times.append(t)
        print(f"Query Run {i+1}: {t:.4f} sec")

    avg_query = sum(query_times) / len(query_times)
    print(f"\n✅ Avg Query Time: {avg_query:.4f} sec")

    # ---- 2. Connection (reconnect) time ----
    conn_times = []

    for i in range(iterations):
        connection.close()  # force reconnect

        start = time.time()
        connection.ensure_connection()
        end = time.time()

        t = end - start
        conn_times.append(t)
        print(f"Reconnect Run {i+1}: {t:.4f} sec")

    avg_conn = sum(conn_times) / len(conn_times)
    print(f"\n✅ Avg Connection Time: {avg_conn:.4f} sec")

    # ---- Final Summary ----
    print("\n📊 FINAL RESULT")
    print(f"Avg Query Time      : {avg_query:.4f} sec")
    print(f"Avg Connection Time : {avg_conn:.4f} sec")

    # ---- Quick Diagnosis ----
    if avg_query > 0.1:
        print("⚠️ DB query latency is HIGH (likely remote DB issue)")
    else:
        print("✅ DB query latency is OK")

    if avg_conn > 0.2:
        print("⚠️ DB connection time is HIGH (network/remote DB)")
    else:
        print("✅ DB connection time is OK")

    print("\n🔍 DB LATENCY TEST END\n")