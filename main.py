import os
import json
import io
import logging
import re
import random
from fastapi import FastAPI, File, UploadFile, HTTPException, Form, Depends, Query
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from openai import OpenAI
from dotenv import load_dotenv
import pdfplumber
from pydantic import BaseModel, ValidationError
from typing import Optional
from sqlalchemy import select, or_, func
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session
from db import get_db, init_db, check_db
from models import (
    Profile as ProfileModel,
    SkillGroup as SkillGroupModel,
    Skill as SkillModel,
    Experience as ExperienceModel,
    ExperienceBullet as ExperienceBulletModel,
    Project as ProjectModel,
    ProjectTech as ProjectTechModel,
    Education as EducationModel,
    Certification as CertificationModel,
    Achievement as AchievementModel,
    Language as LanguageModel,
    AdditionalInfo as AdditionalInfoModel,
    ProfileSearch as ProfileSearchModel,
)

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
DEBUG = os.getenv("DEBUG", "1") == "1"

app = FastAPI(title="DropFolio API", version="1.0.0")

init_db()

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


USERNAME_REGEX = re.compile(r"^[a-z0-9_]{3,20}$")


def normalize_username(raw: str) -> str:
    return raw.strip().lower()


def validate_username(username: str) -> None:
    if not USERNAME_REGEX.match(username):
        raise HTTPException(
            status_code=400,
            detail="Username must be 3-20 characters and use only lowercase letters, numbers, or underscores.",
        )


def is_username_available(db: Session, username: str) -> bool:
    stmt = select(ProfileModel.id).where(ProfileModel.username == username)
    return db.execute(stmt).scalar_one_or_none() is None


def generate_alternatives(db: Session, base: str, count: int = 3) -> list[str]:
    suggestions: list[str] = []
    attempts = 0
    while len(suggestions) < count and attempts < 50:
        attempts += 1
        suffix = random.randint(10, 999)
        candidate = f"{base}{suffix}"
        if len(candidate) > 20:
            candidate = candidate[:20]
        if candidate not in suggestions and is_username_available(db, candidate):
            suggestions.append(candidate)
    return suggestions

# ─── Pydantic models for validation ──────────────────────────────────────────

class PersonalLinks(BaseModel):
    github: Optional[str] = None
    linkedin: Optional[str] = None
    portfolio: Optional[str] = None

class PersonalInfo(BaseModel):
    full_name: Optional[str] = "Unknown"
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
    if not check_db():
        raise HTTPException(status_code=503, detail="Database unavailable.")
    return {"status": "ok", "db": "ok"}


@app.exception_handler(SQLAlchemyError)
async def sqlalchemy_exception_handler(_, exc: SQLAlchemyError):
    logger.exception("Database error")
    if DEBUG:
        detail = f"Database error: {type(exc).__name__}: {str(exc)}"
        payload = {"detail": detail, "error": type(exc).__name__, "message": str(exc)}
    else:
        payload = {"detail": "Database error."}
    return JSONResponse(status_code=503, content=payload)


@app.exception_handler(Exception)
async def unhandled_exception_handler(_, exc: Exception):
    logger.exception("Unhandled error")
    if DEBUG:
        detail = f"Internal server error: {type(exc).__name__}: {str(exc)}"
        payload = {"detail": detail, "error": type(exc).__name__, "message": str(exc)}
    else:
        payload = {"detail": "Internal server error."}
    return JSONResponse(status_code=500, content=payload)


@app.get("/api/usernames/check")
def check_username(
    username: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
):
    normalized = normalize_username(username)
    validate_username(normalized)

    available = is_username_available(db, normalized)
    alternatives: list[str] = []
    if not available:
        alternatives = generate_alternatives(db, normalized, count=3)

    return {"username": normalized, "available": available, "alternatives": alternatives}


@app.get("/api/profiles/{username}")
def get_profile(username: str, db: Session = Depends(get_db)):
    normalized = normalize_username(username)
    stmt = select(ProfileModel).where(ProfileModel.username == normalized)
    profile = db.execute(stmt).scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found.")
    return {"data": profile.raw_payload}


@app.get("/api/search")
def search_profiles(q: str = Query("", min_length=0), db: Session = Depends(get_db)):
    query = q.strip()
    if not query:
        return {"results": []}

    pattern = f"%{query.lower()}%"
    stmt = (
        select(ProfileModel)
        .where(
            or_(
                func.lower(ProfileModel.username).like(pattern),
                func.lower(ProfileModel.full_name).like(pattern),
            )
        )
        .order_by(ProfileModel.username.asc())
        .limit(20)
    )
    profiles = db.execute(stmt).scalars().all()

    results = []
    for profile in profiles:
        results.append(
            {
                "username": profile.username,
                "full_name": profile.full_name,
                "headline": profile.headline,
                "location": profile.location,
                "profile_image_url": profile.profile_image_url,
            }
        )

    return {"results": results}


@app.post("/api/upload")
async def upload_resume(
    username: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    normalized = normalize_username(username)
    validate_username(normalized)

    if not is_username_available(db, normalized):
        raise HTTPException(status_code=409, detail="Username already taken.")

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
        raw_data["username"] = normalized
        portfolio = PortfolioData(**raw_data)
    except ValidationError as e:
        logger.error(f"Validation failed: {e}")
        raise HTTPException(status_code=422, detail=f"LLM returned invalid data structure: {e.errors()}")

    # 4. Save to DB
    pi = portfolio.personal_info
    profile = ProfileModel(
        username=normalized,
        full_name=pi.full_name,
        headline=pi.headline,
        email=pi.email,
        phone=pi.phone,
        location=pi.location,
        profile_image_url=pi.profile_image_url,
        summary=pi.summary,
        raw_payload=portfolio.model_dump(),
    )
    try:
        db.add(profile)
        db.flush()

        # Skills
        for group in portfolio.skills:
            sg = SkillGroupModel(profile_id=profile.id, category=group.category)
            db.add(sg)
            db.flush()
            for item in group.items:
                if item:
                    db.add(SkillModel(skill_group_id=sg.id, name=item))

        # Experience
        for item in portfolio.experience:
            exp = ExperienceModel(
                profile_id=profile.id,
                company_name=item.company_name,
                role=item.role,
                start_date=item.start_date,
                end_date=item.end_date,
                location=item.location,
            )
            db.add(exp)
            db.flush()
            for bullet in item.description:
                if bullet:
                    db.add(ExperienceBulletModel(experience_id=exp.id, bullet=bullet))

        # Projects
        for proj in portfolio.projects:
            pr = ProjectModel(
                profile_id=profile.id,
                title=proj.title,
                role=proj.role,
                description=proj.description,
                live_url=proj.live_url,
                github_url=proj.github_url,
                start_date=proj.start_date,
                end_date=proj.end_date,
                context=proj.context,
            )
            db.add(pr)
            db.flush()
            for tech in proj.tech_stack:
                if tech:
                    db.add(ProjectTechModel(project_id=pr.id, tech=tech))

        # Education
        for edu in portfolio.education:
            db.add(
                EducationModel(
                    profile_id=profile.id,
                    institution=edu.institution,
                    degree=edu.degree,
                    cgpa=edu.cgpa,
                    percentage=edu.percentage,
                    start_date=edu.start_date,
                    end_date=edu.end_date,
                )
            )

        # Certifications
        for cert in portfolio.certifications:
            db.add(
                CertificationModel(
                    profile_id=profile.id,
                    name=cert.name,
                    issuer=cert.issuer,
                    url=cert.url,
                )
            )

        # Achievements
        for ach in portfolio.achievements:
            if ach:
                db.add(AchievementModel(profile_id=profile.id, text=ach))

        # Languages
        for lang in portfolio.languages:
            db.add(LanguageModel(profile_id=profile.id, language=lang.language, proficiency=lang.proficiency))

        # Additional Info
        for info in portfolio.additional_info:
            if info:
                db.add(AdditionalInfoModel(profile_id=profile.id, text=info))

        searchable_text = " ".join(
            [
                normalized,
                pi.full_name or "",
                pi.headline or "",
                pi.location or "",
            ]
        ).strip()
        db.add(
            ProfileSearchModel(
                profile_id=profile.id,
                username=normalized,
                full_name=pi.full_name,
                searchable_text=searchable_text or normalized,
            )
        )

        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Username already taken.")
    except SQLAlchemyError as e:
        db.rollback()
        logger.exception("Database write failed")
        raise HTTPException(status_code=503, detail="Database error.")
    except Exception:
        db.rollback()
        logger.exception("Unexpected error during upload")
        raise

    return {"data": portfolio.model_dump()}
