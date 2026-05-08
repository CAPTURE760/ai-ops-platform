import os
import time
import logging
import sqlite3
import threading
import subprocess
from contextlib import contextmanager
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import psutil
import docker

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI()

cors_origins = os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_methods=["GET", "POST"],
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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                source TEXT NOT NULL,
                level TEXT DEFAULT 'info',
                message TEXT NOT NULL,
                container TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_source ON logs(source)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_level ON logs(level)")
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

# --- Log collector (every 30s) ---

_seen_logs = set()  # Track seen log hashes to avoid duplicates
MAX_SEEN_LOGS = 10000

def _log_hash(source, message, container=None):
    """Generate a simple hash for log deduplication."""
    return hash((source, message[:200], container))

def collect_logs():
    """Collect Docker container logs and service logs."""
    global _seen_logs
    while True:
        time.sleep(30)
        try:
            logs_to_insert = []
            now = time.time()

            # Collect Docker container logs
            try:
                client = docker.from_env()
                for container in client.containers.list():
                    try:
                        raw_logs = container.logs(tail=20, timestamps=True, since=int(now) - 60)
                        for line in raw_logs.decode("utf-8", errors="replace").strip().split("\n"):
                            if not line:
                                continue
                            h = _log_hash("docker", line, container.name)
                            if h in _seen_logs:
                                continue
                            _seen_logs.add(h)
                            # Parse timestamp if present
                            ts = now
                            if line[:20].replace('-', '').replace('T', '').replace(':', '').replace('.', '').isdigit():
                                try:
                                    ts_str = line[:30].split('.')[0]
                                    # Simple timestamp extraction
                                except:
                                    pass
                            # Detect log level
                            level = "info"
                            line_lower = line.lower()
                            if any(w in line_lower for w in ["error", "exception", "traceback", "fatal", "critical"]):
                                level = "error"
                            elif any(w in line_lower for w in ["warning", "warn"]):
                                level = "warning"
                            logs_to_insert.append((ts, "docker", level, line[:1000], container.name))
                    except Exception:
                        continue
            except Exception:
                logger.debug("Docker log collection skipped (Docker unavailable)")

            # Collect service logs (from our own logger)
            # We'll capture recent log entries from the logging handler
            # This is done by reading our own log output

            # Insert collected logs
            if logs_to_insert:
                with get_db() as conn:
                    conn.executemany(
                        "INSERT INTO logs (ts, source, level, message, container) VALUES (?, ?, ?, ?, ?)",
                        logs_to_insert
                    )
                    conn.commit()

            # Cleanup old seen logs to prevent memory leak
            if len(_seen_logs) > MAX_SEEN_LOGS:
                _seen_logs = set(list(_seen_logs)[-MAX_SEEN_LOGS // 2:])

        except Exception:
            logger.exception("collect_logs failed")

threading.Thread(target=collect_logs, daemon=True).start()

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


@app.post("/docker/{container_id}/{action}")
def docker_action(container_id: str, action: str):
    if action not in ("start", "stop", "restart"):
        raise HTTPException(status_code=400, detail="Invalid action")
    try:
        client = docker.from_env()
        container = client.containers.get(container_id)
        getattr(container, action)()
        return {"status": "ok", "action": action, "container": container.name}
    except docker.errors.NotFound:
        raise HTTPException(status_code=404, detail="Container not found")
    except Exception as e:
        logger.exception("docker action error")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/docker/{container_id}/logs")
def docker_logs(container_id: str, tail: int = 100):
    try:
        client = docker.from_env()
        container = client.containers.get(container_id)
        logs = container.logs(tail=tail, timestamps=True).decode("utf-8", errors="replace")
        return {"logs": logs}
    except docker.errors.NotFound:
        raise HTTPException(status_code=404, detail="Container not found")
    except Exception as e:
        logger.exception("docker logs error")
        raise HTTPException(status_code=500, detail=str(e))


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


# --- Log Endpoints ---

@app.get("/logs")
def get_logs(source: str = None, level: str = None, keyword: str = None, limit: int = 100, offset: int = 0):
    query = "SELECT ts, source, level, message, container FROM logs WHERE 1=1"
    params = []
    if source:
        query += " AND source = ?"
        params.append(source)
    if level:
        query += " AND level = ?"
        params.append(level)
    if keyword:
        query += " AND message LIKE ?"
        params.append(f"%{keyword}%")
    query += " ORDER BY ts DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    with get_db() as conn:
        rows = conn.execute(query, params).fetchall()
    logs = [
        {"ts": r[0], "source": r[1], "level": r[2], "message": r[3], "container": r[4]}
        for r in rows
    ]
    return {"logs": logs, "total": len(logs)}


@app.get("/logs/search")
def search_logs(keyword: str, source: str = None, limit: int = 50):
    query = "SELECT ts, source, level, message, container FROM logs WHERE message LIKE ?"
    params = [f"%{keyword}%"]
    if source:
        query += " AND source = ?"
        params.append(source)
    query += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)
    with get_db() as conn:
        rows = conn.execute(query, params).fetchall()
    logs = [
        {"ts": r[0], "source": r[1], "level": r[2], "message": r[3], "container": r[4]}
        for r in rows
    ]
    return {"logs": logs, "keyword": keyword}


@app.get("/logs/stats")
def get_log_stats():
    with get_db() as conn:
        # Count by source
        source_rows = conn.execute(
            "SELECT source, COUNT(*) FROM logs GROUP BY source"
        ).fetchall()
        # Count by level
        level_rows = conn.execute(
            "SELECT level, COUNT(*) FROM logs GROUP BY level"
        ).fetchall()
        # Count errors by source
        error_rows = conn.execute(
            "SELECT source, COUNT(*) FROM logs WHERE level = 'error' GROUP BY source"
        ).fetchall()
    return {
        "by_source": {r[0]: r[1] for r in source_rows},
        "by_level": {r[0]: r[1] for r in level_rows},
        "errors_by_source": {r[0]: r[1] for r in error_rows},
    }


# --- Automation Ops Endpoints ---

PRESET_COMMANDS = [
    {"id": "disk_check", "name": "磁盘检查", "cmd": "df -h", "desc": "查看磁盘使用情况，显示各分区容量和已用空间"},
    {"id": "mem_check", "name": "内存检查", "cmd": "free -h", "desc": "查看内存使用情况，包括物理内存和交换分区"},
    {"id": "process_top", "name": "进程监控", "cmd": "ps aux --sort=-%cpu | head -15", "desc": "查看 CPU 占用最高的前 15 个进程"},
    {"id": "docker_ps", "name": "容器列表", "cmd": "docker ps -a --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'", "desc": "列出所有 Docker 容器及其状态"},
    {"id": "docker_stats", "name": "容器资源", "cmd": "docker stats --no-stream --format 'table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}'", "desc": "查看运行中容器的 CPU 和内存使用"},
    {"id": "network_check", "name": "网络检查", "cmd": "ss -tuln", "desc": "查看所有监听的 TCP/UDP 端口"},
    {"id": "uptime_check", "name": "运行时间", "cmd": "uptime", "desc": "查看系统运行时间和平均负载"},
    {"id": "disk_io", "name": "磁盘 IO", "cmd": "iostat -x 1 2 | tail -20", "desc": "查看磁盘读写性能指标"},
    {"id": "net_connections", "name": "网络连接", "cmd": "ss -s", "desc": "查看网络连接统计摘要"},
    {"id": "kernel_logs", "name": "内核日志", "cmd": "dmesg --level=err,warn | tail -20", "desc": "查看最近的内核错误和警告"},
]

BLOCKED_PATTERNS = [
    "rm -rf /", "rm -rf /*", "dd if=", "mkfs", "fdisk",
    "> /dev/sd", "chmod 777 /", "chown root",
    ":(){ :|:& };:", "shutdown", "reboot", "halt", "poweroff",
    "mkfs.ext4", "mkfs.xfs", "wipefs",
]

@app.get("/ops/presets")
def get_preset_commands():
    return {"presets": PRESET_COMMANDS}


@app.post("/ops/execute")
def execute_command(body: dict):
    cmd = body.get("cmd", "").strip()
    preset_id = body.get("preset_id", "").strip()

    # If preset_id provided, look up the command
    if preset_id:
        preset = next((p for p in PRESET_COMMANDS if p["id"] == preset_id), None)
        if not preset:
            raise HTTPException(status_code=404, detail="Preset command not found")
        cmd = preset["cmd"]

    if not cmd:
        raise HTTPException(status_code=400, detail="No command provided")

    # Security check: block dangerous commands
    cmd_lower = cmd.lower()
    for pattern in BLOCKED_PATTERNS:
        if pattern in cmd_lower:
            raise HTTPException(status_code=403, detail=f"命令被拒绝：包含危险操作 '{pattern}'")

    # Execute with timeout
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=30
        )
        return {
            "cmd": cmd,
            "stdout": result.stdout,
            "returncode": result.returncode,
            "success": result.returncode == 0,
        }
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=408, detail="命令执行超时（30秒）")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"执行失败: {str(e)}")
