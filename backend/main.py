import os
import time
import logging
import sqlite3
import threading
from contextlib import contextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import psutil
import docker

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI()

cors_origins = os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_methods=["GET"],
    allow_headers=["*"],
)

DB_PATH = "/data/ops.db" if psutil.os.path.exists("/data") else "ops.db"

# --- Database ---

def init_db():
    with get_db() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                cpu REAL,
                memory REAL,
                disk REAL,
                net_sent REAL,
                net_recv REAL
            )
        """)
        conn.commit()

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    try:
        yield conn
    finally:
        conn.close()

init_db()

# --- Background recorder (every 60s) ---

_net_lock = threading.Lock()
_prev_net = psutil.net_io_counters()

def record_metrics():
    global _prev_net
    while True:
        time.sleep(60)
        try:
            cpu = psutil.cpu_percent(interval=1)
            mem = psutil.virtual_memory().percent
            disk = psutil.disk_usage("/").percent
            net = psutil.net_io_counters()
            with _net_lock:
                sent_speed = (net.bytes_sent - _prev_net.bytes_sent) / 60
                recv_speed = (net.bytes_recv - _prev_net.bytes_recv) / 60
                _prev_net = net
            with get_db() as conn:
                conn.execute(
                    "INSERT INTO metrics (ts, cpu, memory, disk, net_sent, net_recv) VALUES (?, ?, ?, ?, ?, ?)",
                    (time.time(), cpu, mem, disk, sent_speed, recv_speed),
                )
                conn.commit()
        except Exception:
            logger.exception("record_metrics failed")

threading.Thread(target=record_metrics, daemon=True).start()

# --- Endpoints ---

@app.get("/")
def root():
    return {"message": "AI Ops Platform 运行中"}


@app.get("/system")
def get_system_info():
    return {
        "cpu": psutil.cpu_percent(interval=0.5),
        "memory": psutil.virtual_memory().percent,
        "disk": psutil.disk_usage("/").percent,
    }


@app.get("/network")
def get_network():
    global _prev_net
    net = psutil.net_io_counters()
    with _net_lock:
        sent_speed = net.bytes_sent - _prev_net.bytes_sent
        recv_speed = net.bytes_recv - _prev_net.bytes_recv
        _prev_net = net
    return {
        "bytes_sent": net.bytes_sent,
        "bytes_recv": net.bytes_recv,
        "sent_speed": sent_speed,
        "recv_speed": recv_speed,
    }


@app.get("/docker")
def get_docker_containers():
    try:
        client = docker.from_env()
        containers = []
        for c in client.containers.list(all=True):
            containers.append({
                "name": c.name,
                "status": c.status,
                "image": c.image.tags[0] if c.image.tags else str(c.image.id)[:12],
                "id": c.short_id,
            })
        return {"containers": containers}
    except docker.errors.DockerException:
        return {"containers": [], "error": "Docker service unavailable"}
    except Exception:
        logger.exception("docker endpoint error")
        return {"containers": [], "error": "Internal error"}


@app.get("/processes")
def get_top_processes():
    procs = []
    for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
        try:
            info = p.info
            procs.append(info)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    procs.sort(key=lambda x: x.get("cpu_percent", 0) or 0, reverse=True)
    return {"processes": procs[:5]}


@app.get("/history")
def get_history():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT ts, cpu, memory, disk, net_sent, net_recv FROM metrics ORDER BY ts DESC LIMIT 60"
        ).fetchall()
    history = [
        {"ts": r[0], "cpu": r[1], "memory": r[2], "disk": r[3], "net_sent": r[4], "net_recv": r[5]}
        for r in reversed(rows)
    ]
    return {"history": history}
