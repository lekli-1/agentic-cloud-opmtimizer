from fastapi import FastAPI
import math

app = FastAPI()

@app.get("/health")
def health():
    return {"status": "healthy"}

@app.get("/stress")
def stress():
    # Heavy calculation to burn CPU cycles
    x = 0.0001
    for i in range(5000000):
        x += math.sqrt(x)
    return {"message": "CPU stressed", "result": x}
