import os
import time
import logging
import sqlite3
import threading
import subprocess
from contextlib import contextmanager
from datetime import datetime
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
    {"id": "disk_check", "name": "磁盘检查", "desc": "查看磁盘使用情况，显示各分区容量和已用空间"},
    {"id": "mem_check", "name": "内存检查", "desc": "查看内存使用情况，包括物理内存和交换分区"},
    {"id": "process_top", "name": "进程监控", "desc": "查看 CPU 占用最高的前 15 个进程"},
    {"id": "docker_ps", "name": "容器列表", "desc": "列出所有 Docker 容器及其状态"},
    {"id": "docker_stats", "name": "容器资源", "desc": "查看运行中容器的 CPU 和内存使用"},
    {"id": "network_check", "name": "网络检查", "desc": "查看所有监听的 TCP/UDP 端口"},
    {"id": "uptime_check", "name": "运行时间", "desc": "查看系统运行时间和平均负载"},
    {"id": "disk_io", "name": "磁盘 IO", "desc": "查看磁盘读写性能指标"},
    {"id": "net_connections", "name": "网络连接", "desc": "查看网络连接统计摘要"},
    {"id": "kernel_logs", "name": "内核日志", "desc": "查看最近的内核错误和警告"},
]


def _fmt_bytes(b):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024


def _run_preset(preset_id: str) -> dict:
    if preset_id == "disk_check":
        lines = ["文件系统                容量  已用  可用  使用%  挂载点"]
        for part in psutil.disk_partitions():
            try:
                u = psutil.disk_usage(part.mountpoint)
                lines.append(f"{part.device:20s}  {_fmt_bytes(u.total):>8s}  {_fmt_bytes(u.used):>8s}  {_fmt_bytes(u.free):>8s}  {u.percent:5.1f}%  {part.mountpoint}")
            except PermissionError:
                continue
        return {"output": "\n".join(lines)}

    elif preset_id == "mem_check":
        vm = psutil.virtual_memory()
        sw = psutil.swap_memory()
        lines = [
            "              总量      已用      可用    使用%",
            f"物理内存  {_fmt_bytes(vm.total):>8s}  {_fmt_bytes(vm.used):>8s}  {_fmt_bytes(vm.available):>8s}  {vm.percent:5.1f}%",
            f"交换分区  {_fmt_bytes(sw.total):>8s}  {_fmt_bytes(sw.used):>8s}  {_fmt_bytes(sw.free):>8s}  {sw.percent:5.1f}%",
        ]
        return {"output": "\n".join(lines)}

    elif preset_id == "process_top":
        procs = []
        for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent", "username"]):
            try:
                procs.append(p.info)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        procs.sort(key=lambda x: x.get("cpu_percent", 0) or 0, reverse=True)
        lines = [f"{'PID':>7s}  {'USER':12s}  {'CPU%':>6s}  {'MEM%':>6s}  {'NAME':20s}"]
        for p in procs[:15]:
            lines.append(f"{p['pid']:>7d}  {(p.get('username') or '-')[:12]:12s}  {(p.get('cpu_percent') or 0):5.1f}%  {(p.get('memory_percent') or 0):5.1f}%  {(p.get('name') or '-')[:20]:20s}")
        return {"output": "\n".join(lines)}

    elif preset_id == "docker_ps":
        try:
            client = docker.from_env()
            lines = [f"{'名称':20s}  {'状态':12s}  {'镜像':30s}  {'ID':12s}"]
            for c in client.containers.list(all=True):
                image = c.image.tags[0] if c.image.tags else str(c.image.id)[:12]
                lines.append(f"{c.name:20s}  {c.status:12s}  {image[:30]:30s}  {c.short_id:12s}")
            return {"output": "\n".join(lines)}
        except Exception as e:
            return {"output": f"Docker 不可用: {e}", "error": True}

    elif preset_id == "docker_stats":
        try:
            client = docker.from_env()
            lines = [f"{'名称':20s}  {'CPU%':>8s}  {'内存使用':15s}  {'状态':12s}"]
            for c in client.containers.list():
                try:
                    stats = c.stats(stream=False)
                    cpu_delta = stats["cpu_stats"]["cpu_usage"]["total_usage"] - stats["precpu_stats"]["cpu_usage"]["total_usage"]
                    system_delta = stats["cpu_stats"]["system_cpu_usage"] - stats["precpu_stats"]["system_cpu_usage"]
                    cpu_pct = (cpu_delta / system_delta * len(stats["cpu_stats"]["cpu_usage"]["percpu_usage"]) * 100) if system_delta > 0 else 0
                    mem_usage = stats["memory_stats"].get("usage", 0)
                    mem_limit = stats["memory_stats"].get("limit", 0)
                    lines.append(f"{c.name:20s}  {cpu_pct:>7.2f}%  {_fmt_bytes(mem_usage):>8s}/{_fmt_bytes(mem_limit):>8s}  {c.status:12s}")
                except Exception:
                    lines.append(f"{c.name:20s}  {'N/A':>8s}  {'N/A':>15s}  {c.status:12s}")
            return {"output": "\n".join(lines)}
        except Exception as e:
            return {"output": f"Docker 不可用: {e}", "error": True}

    elif preset_id == "network_check":
        lines = [f"{'协议':6s}  {'本地地址':30s}  {'状态':12s}  {'进程':15s}"]
        for conn in psutil.net_connections(kind="inet"):
            if conn.status == "LISTEN":
                laddr = f"{conn.laddr.ip}:{conn.laddr.port}" if conn.laddr else "-"
                proc_name = "-"
                if conn.pid:
                    try:
                        proc_name = psutil.Process(conn.pid).name()
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        proc_name = f"pid:{conn.pid}"
                proto = "tcp" if conn.type == 1 else "udp"
                lines.append(f"{proto:6s}  {laddr:30s}  {conn.status:12s}  {proc_name:15s}")
        return {"output": "\n".join(lines) if len(lines) > 1 else "没有监听中的端口"}

    elif preset_id == "uptime_check":
        boot = datetime.fromtimestamp(psutil.boot_time())
        uptime_sec = time.time() - psutil.boot_time()
        days, rem = divmod(int(uptime_sec), 86400)
        hours, rem = divmod(rem, 3600)
        mins, _ = divmod(rem, 60)
        try:
            load = os.getloadavg()
            load_str = f"负载: {load[0]:.2f}, {load[1]:.2f}, {load[2]:.2f}"
        except (OSError, AttributeError):
            load_str = "负载: N/A"
        return {"output": f"启动时间: {boot.strftime('%Y-%m-%d %H:%M:%S')}\n运行时长: {days}天 {hours}小时 {mins}分钟\n{load_str}"}

    elif preset_id == "disk_io":
        io = psutil.disk_io_counters(perdisk=True)
        lines = [f"{'设备':10s}  {'读次数':>10s}  {'写次数':>10s}  {'读取':>10s}  {'写入':>10s}"]
        for dev, counters in io.items():
            if dev.startswith("loop"):
                continue
            lines.append(f"{dev:10s}  {counters.read_count:>10d}  {counters.write_count:>10d}  {_fmt_bytes(counters.read_bytes):>10s}  {_fmt_bytes(counters.write_bytes):>10s}")
        return {"output": "\n".join(lines)}

    elif preset_id == "net_connections":
        conns = psutil.net_connections(kind="inet")
        status_count = {}
        for c in conns:
            status_count[c.status] = status_count.get(c.status, 0) + 1
        lines = [f"{'状态':15s}  {'数量':>6s}"]
        for status, count in sorted(status_count.items(), key=lambda x: -x[1]):
            lines.append(f"{status:15s}  {count:>6d}")
        lines.append(f"\n总计: {len(conns)} 个连接")
        return {"output": "\n".join(lines)}

    elif preset_id == "kernel_logs":
        try:
            result = subprocess.run(
                ["dmesg", "--level=err,warn"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode != 0:
                # Try reading from journalctl
                result = subprocess.run(
                    ["journalctl", "-p", "warning", "-n", "20", "--no-pager"],
                    capture_output=True, text=True, timeout=5
                )
            if result.returncode == 0 and result.stdout.strip():
                lines = result.stdout.strip().split("\n")[-20:]
                return {"output": "\n".join(lines)}
            # Fallback: read /var/log/kern.log or /var/log/messages
            for logfile in ["/var/log/kern.log", "/var/log/messages", "/var/log/syslog"]:
                if os.path.exists(logfile):
                    try:
                        with open(logfile, "r") as f:
                            lines = f.readlines()[-20:]
                        return {"output": "".join(lines).strip() or "无内核日志"}
                    except PermissionError:
                        continue
            return {"output": "无法读取内核日志（权限不足或日志文件不存在）"}
        except Exception as e:
            return {"output": f"读取内核日志失败: {e}", "error": True}

    return {"output": "未知的预设命令", "error": True}


@app.get("/ops/presets")
def get_preset_commands():
    return {"presets": PRESET_COMMANDS}


@app.post("/ops/execute")
def execute_command(body: dict):
    preset_id = body.get("preset_id", "").strip()

    if preset_id:
        preset = next((p for p in PRESET_COMMANDS if p["id"] == preset_id), None)
        if not preset:
            raise HTTPException(status_code=404, detail="预设命令不存在")
        try:
            result = _run_preset(preset_id)
            return {
                "preset_id": preset_id,
                "name": preset["name"],
                "output": result.get("output", ""),
                "success": not result.get("error", False),
            }
        except Exception as e:
            logger.exception(f"preset {preset_id} failed")
            raise HTTPException(status_code=500, detail=f"执行失败: {str(e)}")

    # Custom commands are no longer supported for security reasons
    raise HTTPException(status_code=400, detail="请使用预设命令，自定义命令已禁用")
