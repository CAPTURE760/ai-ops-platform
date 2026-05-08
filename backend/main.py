from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import psutil

app = FastAPI()

# 添加这段
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

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