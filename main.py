import os
import json
import io
import logging
from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from openai import OpenAI
from dotenv import load_dotenv
import pdfplumber
from pydantic import BaseModel, ValidationError
from typing import Optional

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="DropFolio API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Lazy client — initialized on first use so a bad install doesn't crash startup
_openai_client: OpenAI | None = None

def get_client() -> OpenAI:
    global _openai_client
    if _openai_client is None:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not set in backend/.env")
        _openai_client = OpenAI(api_key=api_key)
    return _openai_client

# ─── Pydantic models for validation ──────────────────────────────────────────

class PersonalLinks(BaseModel):
    github: Optional[str] = None
    linkedin: Optional[str] = None
    portfolio: Optional[str] = None

class PersonalInfo(BaseModel):
    full_name: str
    headline: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    location: Optional[str] = None
    profile_image_url: Optional[str] = None
    links: PersonalLinks
    summary: Optional[str] = None

class ExperienceItem(BaseModel):
    company_name: Optional[str] = None
    role: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    location: Optional[str] = None
    description: list[str]

class ProjectItem(BaseModel):
    title: Optional[str] = None
    role: Optional[str] = None
    description: Optional[str] = None
    tech_stack: list[str]
    live_url: Optional[str] = None
    github_url: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    context: Optional[str] = None

class EducationItem(BaseModel):
    institution: Optional[str] = None
    degree: Optional[str] = None
    cgpa: Optional[str] = None
    percentage: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None

class LanguageItem(BaseModel):
    language: str
    proficiency: Optional[str] = None

class CertificationItem(BaseModel):
    name: Optional[str] = None
    issuer: Optional[str] = None
    url: Optional[str] = None

class SkillGroup(BaseModel):
    category: str
    items: list[str]

class PortfolioData(BaseModel):
    username: str
    personal_info: PersonalInfo
    skills: list[SkillGroup]
    experience: list[ExperienceItem]
    projects: list[ProjectItem]
    education: list[EducationItem]
    certifications: list[CertificationItem]
    achievements: list[str]
    languages: list[LanguageItem] = []
    additional_info: list[str] = []

# ─── Prompt ──────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are an expert resume parser. Your ONLY job is to extract information from a resume and return it as a single valid JSON object.

STRICT RULES:
1. Return ONLY a JSON object — no markdown, no code fences, no explanations.
2. The JSON must conform EXACTLY to the schema below.
3. For the "username" field: use the person's first name in lowercase (e.g. "john"). If first name unavailable, use the email prefix.
4. For missing optional fields, use null — never omit them.
5. For arrays that have no data (no projects, no certifications, etc.), return an empty array [].
6. "end_date" in experience should be "Present" if the role is current.
7. Handle multi-column resume layouts — do NOT mix up columns. Carefully separate work experience from projects, skills, and education.
8. The "description" field in each experience item must be an array of individual bullet-point strings (not one long block of text).
9. Skills must be grouped by their category headers found in the resume (e.g. "Languages", "Frameworks", "Tools"). If no categories exist, use a single group: {"category": "Skills", "items": [...]}.
10. For GitHub and LinkedIn links: extract the FULL exact URL strings if present (e.g. 'https://github.com/username'), do NOT just extract the words 'GitHub' or 'LinkedIn'.
11. In projects: separate the project title from the person's role (e.g. 'Project Lead').
12. Extract CGPA/GPA for education if present. Also extract percentage scores (e.g. 79.8%) into the 'percentage' field — especially for school-level education (CBSE, ICSE, SSC, etc.).
13. Extract all honors, awards, hackathon wins, or standalone accomplishments into the 'achievements' array.
14. Do NOT invent or fabricate information. Only extract what is present.
15. The "profile_image_url" should always be null unless explicitly listed in the resume text.
16. Extract ALL experience entries, ALL projects, ALL education, ALL certifications — never truncate.
17. Make NO assumptions. If an entire section (e.g. education) is missing, return []. If a specific piece of data (e.g. company name, degree) is missing, return null. The schema must match perfectly, but allow nulls where missing.
18. For projects: extract a start_date and end_date if a timeline is mentioned (e.g. 'Dec 2024 - Feb 2025'). Also extract any context about when/where the project was done (e.g. 'Done during internship at Fulcrum GT') into the 'context' field.
19. Extract spoken/written languages (e.g. English, Telugu) into the 'languages' array with their proficiency level. Do NOT mix spoken languages with programming languages.
20. Extract any additional information about the candidate's availability, willingness to relocate, or openness to remote work into the 'additional_info' array as individual strings.

JSON Schema:
{
  "username": "string",
  "personal_info": {
    "full_name": "string",
    "headline": "string | null",
    "email": "string | null",
    "phone": "string | null",
    "location": "string | null",
    "profile_image_url": null,
    "links": {
      "github": "string | null",
      "linkedin": "string | null",
      "portfolio": "string | null"
    },
    "summary": "string | null"
  },
  "skills": [
    {
      "category": "string",
      "items": ["string"]
    }
  ],
  "experience": [
    {
      "company_name": "string | null",
      "role": "string | null",
      "start_date": "string | null",
      "end_date": "string or 'Present' | null",
      "location": "string | null",
      "description": ["string"]
    }
  ],
  "projects": [
    {
      "title": "string | null",
      "role": "string | null",
      "description": "string | null",
      "tech_stack": ["string"],
      "live_url": "string | null",
      "github_url": "string | null",
      "start_date": "string | null",
      "end_date": "string | null",
      "context": "string | null"
    }
  ],
  "education": [
    {
      "institution": "string | null",
      "degree": "string | null",
      "cgpa": "string | null",
      "percentage": "string | null",
      "start_date": "string | null",
      "end_date": "string | null"
    }
  ],
  "certifications": [
    {
      "name": "string | null",
      "issuer": "string | null",
      "url": "string | null"
    }
  ],
  "achievements": ["string"],
  "languages": [
    {
      "language": "string",
      "proficiency": "string | null"
    }
  ],
  "additional_info": ["string"]
}"""

# ─── Helper ───────────────────────────────────────────────────────────────────

def extract_text_from_pdf(file_bytes: bytes) -> str:
    """Extract all text from a PDF using pdfplumber (multi-column aware)."""
    text_parts = []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text(x_tolerance=3, y_tolerance=3)
            if page_text:
                text_parts.append(page_text)
            
            # Extract underlying embedded hyperlinks (e.g. GitHub/LinkedIn)
            if page.hyperlinks:
                for link in page.hyperlinks:
                    uri = link.get("uri")
                    if uri:
                        text_parts.append(f"[Embedded Link found in PDF]: {uri}")

    return "\n\n".join(text_parts)


def call_llm(resume_text: str) -> dict:
    """Call OpenAI with the full resume text and return parsed JSON."""
    response = get_client().chat.completions.create(
        model="gpt-4o",
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Parse this resume and return the JSON:\n\n{resume_text}"},
        ],
        temperature=0.1,
    )
    raw = response.choices[0].message.content
    return json.loads(raw)


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/upload")
async def upload_resume(file: UploadFile = File(...)):
    # Validate file type
    if file.content_type not in ("application/pdf", "application/octet-stream"):
        if not (file.filename or "").lower().endswith(".pdf"):
            raise HTTPException(status_code=400, detail="Only PDF files are accepted.")

    file_bytes = await file.read()
    if len(file_bytes) == 0:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    if len(file_bytes) > 20 * 1024 * 1024:  # 20 MB limit
        raise HTTPException(status_code=400, detail="File too large. Maximum size is 20MB.")

    # 1. Extract text
    try:
        resume_text = extract_text_from_pdf(file_bytes)
    except Exception as e:
        logger.error(f"PDF extraction failed: {e}")
        raise HTTPException(status_code=422, detail=f"Could not extract text from PDF: {str(e)}")

    if not resume_text.strip():
        raise HTTPException(status_code=422, detail="No readable text found in the PDF. It may be a scanned image PDF.")

    logger.info(f"Extracted {len(resume_text)} characters from PDF.")

    # 2. Call LLM
    try:
        raw_data = call_llm(resume_text)
    except Exception as e:
        logger.error(f"LLM call failed: {e}")
        raise HTTPException(status_code=502, detail=f"LLM extraction failed: {str(e)}")

    # 3. Validate with Pydantic
    try:
        portfolio = PortfolioData(**raw_data)
    except ValidationError as e:
        logger.error(f"Validation failed: {e}")
        raise HTTPException(status_code=422, detail=f"LLM returned invalid data structure: {e.errors()}")

    return {"data": portfolio.model_dump()}
