import os
import json
import ast
import asyncio
import re
from typing import Optional
from fastapi import FastAPI, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
import httpx
from dotenv import load_dotenv
import chromadb
from chromadb.utils import embedding_functions
from sentence_transformers import SentenceTransformer
import github

load_dotenv()

app = FastAPI()

# Mount static and templates
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Load history
HISTORY_FILE = "history.json"
def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r") as f:
            try:
                return json.load(f)
            except:
                return []
    return []

def save_history(history):
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)

# ChromaDB setup
chroma_client = chromadb.PersistentClient(path="chroma_db")
sentence_transformer_ef = embedding_functions.SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")
try:
    collection = chroma_client.get_collection("templates")
except:
    collection = chroma_client.create_collection("templates", embedding_function=sentence_transformer_ef)

# Load templates from library
TEMPLATES_DIR = "templates_library"
def load_templates():
    templates_list = []
    for fname in os.listdir(TEMPLATES_DIR):
        if fname.endswith(".json"):
            with open(os.path.join(TEMPLATES_DIR, fname), "r") as f:
                templates_list.append(json.load(f))
    return templates_list

def init_chromadb():
    # Check if collection already has data
    if collection.count() == 0:
        templates = load_templates()
        for t in templates:
            collection.add(
                documents=[t["description"]],
                metadatas=[{"title": t["title"], "code": json.dumps(t["files"])}],
                ids=[t["title"]]
            )

@app.on_event("startup")
async def startup():
    init_chromadb()

# API key
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
if not DEEPSEEK_API_KEY:
    raise ValueError("DEEPSEEK_API_KEY environment variable not set")

# Helper functions
def repair_json(raw: str) -> str:
    # Remove markdown code blocks
    raw = re.sub(r'```json\s*', '', raw)
    raw = re.sub(r'```\s*', '', raw)
    # Remove trailing commas before closing braces/brackets
    raw = re.sub(r',\s*}', '}', raw)
    raw = re.sub(r',\s*]', ']', raw)
    # Try to parse, if fails, attempt to fix unterminated strings
    try:
        json.loads(raw)
        return raw
    except:
        # Simple fix: add missing quotes (basic)
        raw = re.sub(r'(?<=":)\s*([^"\s}\]]+)(?=\s*[,\}])', r'"\1"', raw)
        return raw

async def call_deepseek(prompt: str, reasoning_effort: str = "low", max_tokens: int = 4096) -> str:
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            "https://api.deepseek.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": prompt}],
                "reasoning_effort": reasoning_effort,
                "max_tokens": max_tokens
            }
        )
        if response.status_code != 200:
            raise HTTPException(status_code=response.status_code, detail=response.text)
        data = response.json()
        return data["choices"][0]["message"]["content"]

async def generate_project(prompt: str, language: str, cancel_event: asyncio.Event) -> dict:
    # Step 1: Fast analysis
    fast_prompt = f"""You are a project generator. Analyze the following request and output a JSON object with keys: detected_language (must be '{language}' if language is specified), framework, project_type, files_needed (list of filenames), main_file, start_command. Do not include any other text.
Request: {prompt}
"""
    try:
        fast_response = await call_deepseek(fast_prompt, "low", 1024)
        fast_response = repair_json(fast_response)
        analysis = json.loads(fast_response)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Fast analysis failed: {str(e)}")

    if cancel_event.is_set():
        raise asyncio.CancelledError()

    # Step 2: Deep code generation
    deep_prompt = f"""Generate a complete project based on the following analysis:
{json.dumps(analysis)}

User request: {prompt}
Output a JSON object with keys: "summary" (string), "files" (list of objects with "path" and "content"). The code should be in {language}.
"""
    try:
        deep_response = await call_deepseek(deep_prompt, "high", 8192)
        deep_response = repair_json(deep_response)
        project = json.loads(deep_response)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Deep generation failed: {str(e)}")

    if cancel_event.is_set():
        raise asyncio.CancelledError()

    # Self-correction for Python
    if language.lower() == "python":
        for i in range(2):
            errors = []
            for f in project["files"]:
                if f["path"].endswith(".py"):
                    try:
                        ast.parse(f["content"])
                    except SyntaxError as e:
                        errors.append((f["path"], str(e)))
            if not errors:
                break
            # Fix errors
            fix_prompt = f"""The following Python files have syntax errors. Fix them and return the entire project JSON with corrected files.
Project: {json.dumps(project)}
Errors: {json.dumps(errors)}
"""
            try:
                fix_response = await call_deepseek(fix_prompt, "high", 8192)
                fix_response = repair_json(fix_response)
                project = json.loads(fix_response)
            except:
                pass

    return project

# Routes
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    history = load_history()
    return templates.TemplateResponse("index.html", {"request": request, "history": history})

@app.post("/generate")
async def generate(prompt: str = Form(...), language: str = Form("Python")):
    # Check template similarity
    results = collection.query(query_texts=[prompt], n_results=1)
    if results["distances"][0][0] < 0.75:
        # Template found
        template = json.loads(results["metadatas"][0][0]["code"])
        return {"template": template, "match": results["metadatas"][0][0]["title"]}
    else:
        return {"template": None}

@app.post("/generate_full")
async def generate_full(prompt: str = Form(...), language: str = Form("Python")):
    cancel_event = asyncio.Event()
    # Store cancel event in app state
    app.state.cancel_event = cancel_event
    try:
        project = await generate_project(prompt, language, cancel_event)
        # Save to history
        history = load_history()
        history.append({"prompt": prompt, "language": language, "project": project, "rating": 0})
        save_history(history)
        return project
    except asyncio.CancelledError:
        return {"cancelled": True}

@app.post("/cancel")
async def cancel():
    if hasattr(app.state, "cancel_event"):
        app.state.cancel_event.set()
        return {"status": "cancelled"}
    return {"status": "no active generation"}

@app.get("/progress")
async def progress():
    async def event_generator():
        # Simulate progress (real implementation would send events from generation)
        for i in range(1, 11):
            yield f"data: {json.dumps({'step': i, 'message': f'Generating file {i}'})}\n\n"
            await asyncio.sleep(0.5)
        yield f"data: {json.dumps({'step': 100, 'message': 'Complete'})}\n\n"
    return StreamingResponse(event_generator(), media_type="text/event-stream")

@app.post("/rate")
async def rate(prompt: str = Form(...), rating: int = Form(...)):
    history = load_history()
    for item in history:
        if item["prompt"] == prompt:
            item["rating"] = rating
            save_history(history)
            return {"status": "success"}
    raise HTTPException(status_code=404, detail="Prompt not found")

@app.post("/push")
async def push(repo_name: str = Form(...), private: bool = Form(False), github_token: str = Form(...)):
    # Validate repo name
    repo_name = repo_name.replace(" ", "-")
    if not repo_name:
        raise HTTPException(status_code=400, detail="Invalid repo name")
    # Push to GitHub (simplified)
    try:
        g = github.Github(github_token)
        user = g.get_user()
        repo = user.create_repo(repo_name, private=private)
        # Create initial commit with all files from last generated project
        # (This is simplified; actual implementation would need to get project from history)
        return {"success": True, "url": repo.html_url}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
