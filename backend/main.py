from fastapi import FastAPI
import psutil

app = FastAPI()

@app.get("/")
def root():
    return {"message": "AI Ops Platform 运行中"}

@app.get("/system")
def get_system_info():
    return {
        "cpu": psutil.cpu_percent(interval=1),
        "memory": psutil.virtual_memory().percent,
        "disk": psutil.disk_usage('/').percent
    }